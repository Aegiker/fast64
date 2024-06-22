import bpy, os, re, mathutils
from typing import Union, Optional
from mathutils import Vector, Matrix
from ..f3d.f3d_parser import F3DContext, F3DTextureReference, getImportData, math_eval
from ..f3d.f3d_material import TextureProperty, createF3DMat, texFormatOf, texBitSizeF3D
from ..utility import PluginError, CData, hexOrDecInt, getNameFromPath, getTextureSuffixFromFormat, toAlnum, unpackNormal, readFile #readFile is TEMPORARY it will not be needed when I am done
from ..f3d.flipbook import TextureFlipbook, FlipbookProperty, usesFlipbook, ootFlipbookReferenceIsValid
from .oot_utility import ootGetArrayCount, captureData

import random #TEMPORARY!

from ..f3d.f3d_writer import VertexGroupInfo, TriangleConverterInfo, F3DVert, BufferVertex, VertexWeight
from ..f3d.f3d_texture_writer import (
    getColorsUsedInImage,
    mergePalettes,
    writeCITextureData,
    writeNonCITextureData,
    getTextureNamesFromImage,
)
from ..f3d.f3d_gbi import (
    FModel,
    FMaterial,
    FImage,
    FImageKey,
    FPaletteKey,
    GfxMatWriteMethod,
    SPDisplayList,
    GfxList,
    GfxListTag,
    DLFormat,
    SPMatrix,
    GfxFormatter,
    MTX_SIZE,
    DPSetTile,
)


# read included asset data
def ootGetIncludedAssetData(basePath: str, currentPaths: list[str], data: str) -> str:
    includeData = ""
    searchedPaths = currentPaths[:]

    print("Included paths:")

    # search assets
    for includeMatch in re.finditer(r"\#include\s*\"(assets/objects/(.*?))\.h\"", data):
        path = os.path.join(basePath, includeMatch.group(1) + ".c")
        if path in searchedPaths:
            continue
        searchedPaths.append(path)
        subIncludeData = getImportData([path]) + "\n"
        includeData += subIncludeData
        print(path)

        for subIncludeMatch in re.finditer(r"\#include\s*\"(((?![/\"]).)*)\.c\"", subIncludeData):
            subPath = os.path.join(os.path.dirname(path), subIncludeMatch.group(1) + ".c")
            if subPath in searchedPaths:
                continue
            searchedPaths.append(subPath)
            print(subPath)
            includeData += getImportData([subPath]) + "\n"

    # search same directory c includes, both in current path and in included object files
    # these are usually fast64 exported files
    for includeMatch in re.finditer(r"\#include\s*\"(((?![/\"]).)*)\.c\"", data):
        sameDirPaths = [
            os.path.join(os.path.dirname(currentPath), includeMatch.group(1) + ".c") for currentPath in currentPaths
        ]
        sameDirPathsToSearch = []
        for sameDirPath in sameDirPaths:
            if sameDirPath not in searchedPaths:
                sameDirPathsToSearch.append(sameDirPath)

        for sameDirPath in sameDirPathsToSearch:
            print(sameDirPath)

        includeData += getImportData(sameDirPathsToSearch) + "\n"
    return includeData


def ootGetActorDataPaths(basePath: str, overlayName: str) -> list[str]:
    actorFilePath = os.path.join(basePath, f"src/overlays/actors/{overlayName}/z_{overlayName[4:].lower()}.c")
    actorFileDataPath = f"{actorFilePath[:-2]}_data.c"  # some bosses store texture arrays here

    return [actorFileDataPath, actorFilePath]


# read actor data
def ootGetActorData(basePath: str, overlayName: str) -> str:
    actorData = getImportData(ootGetActorDataPaths(basePath, overlayName))
    return actorData


def ootGetLinkData(basePath: str) -> str:
    linkFilePath = os.path.join(basePath, f"src/code/z_player_lib.c")
    actorData = getImportData([linkFilePath])

    return actorData


class OOTModel(FModel):
    def __init__(self, name, DLFormat, drawLayerOverride):
        self.drawLayerOverride = drawLayerOverride
        self.flipbooks: list[TextureFlipbook] = []

        FModel.__init__(self, name, DLFormat, GfxMatWriteMethod.WriteAll)

    # Since dynamic textures are handled by scene draw config, flipbooks should only belong to scene model.
    # Thus we have this function.
    def getFlipbookOwner(self):
        if self.parentModel is not None:
            model = self.parentModel
        else:
            model = self
        return model

    def getDrawLayerV3(self, obj):
        return obj.ootDrawLayer

    def getRenderMode(self, drawLayer):
        if self.drawLayerOverride:
            drawLayerUsed = self.drawLayerOverride
        else:
            drawLayerUsed = drawLayer
        defaultRenderModes = bpy.context.scene.world.ootDefaultRenderModes
        cycle1 = getattr(defaultRenderModes, drawLayerUsed.lower() + "Cycle1")
        cycle2 = getattr(defaultRenderModes, drawLayerUsed.lower() + "Cycle2")
        return [cycle1, cycle2]

    def addFlipbookWithRepeatCheck(self, flipbook: TextureFlipbook):
        model = self.getFlipbookOwner()

        def raiseErr(subMsg):
            raise PluginError(
                f"There are two flipbooks {subMsg} trying to write to the same texture array "
                + f"named: {flipbook.name}.\nMake sure that this flipbook name is unique, or "
                + "that repeated uses of this name use the same textures in the same order/format."
            )

        for existingFlipbook in model.flipbooks:
            if existingFlipbook.name == flipbook.name:
                if len(existingFlipbook.textureNames) != len(flipbook.textureNames):
                    raiseErr(
                        f"of different lengths ({len(existingFlipbook.textureNames)} "
                        + f"vs. {len(flipbook.textureNames)})"
                    )
                for i in range(len(flipbook.textureNames)):
                    if existingFlipbook.textureNames[i] != flipbook.textureNames[i]:
                        raiseErr(
                            f"with differing elements (elem {i} = "
                            + f"{existingFlipbook.textureNames[i]} vs. "
                            + f"{flipbook.textureNames[i]})"
                        )
        model.flipbooks.append(flipbook)

    def validateImages(self, material: bpy.types.Material, index: int):
        flipbookProp = getattr(material.flipbookGroup, f"flipbook{index}")
        texProp = getattr(material.f3d_mat, f"tex{index}")
        allImages = []
        refSize = (texProp.tex_reference_size[0], texProp.tex_reference_size[1])
        for flipbookTexture in flipbookProp.textures:
            if flipbookTexture.image is None:
                raise PluginError(f"Flipbook for {material.name} has a texture array item that has not been set.")
            imSize = (flipbookTexture.image.size[0], flipbookTexture.image.size[1])
            if imSize != refSize:
                raise PluginError(
                    f"In {material.name}: texture reference size is {refSize}, "
                    + f"but flipbook image {flipbookTexture.image.filepath} size is {imSize}."
                )
            if flipbookTexture.image not in allImages:
                allImages.append(flipbookTexture.image)
        return allImages

    def processTexRefCITextures(self, fMaterial: FMaterial, material: bpy.types.Material, index: int) -> FImage:
        # print("Processing flipbook...")
        model = self.getFlipbookOwner()
        flipbookProp = getattr(material.flipbookGroup, f"flipbook{index}")
        texProp = getattr(material.f3d_mat, f"tex{index}")
        if not usesFlipbook(material, flipbookProp, index, True, ootFlipbookReferenceIsValid):
            return super().processTexRefCITextures(fMaterial, material, index)
        if len(flipbookProp.textures) == 0:
            raise PluginError(f"{str(material)} cannot have a flipbook material with no flipbook textures.")

        flipbook = TextureFlipbook(flipbookProp.name, flipbookProp.exportMode, [], [])

        pal = []
        allImages = self.validateImages(material, index)
        for flipbookTexture in flipbookProp.textures:
            # print(f"Texture: {str(flipbookTexture.image)}")
            imageName, filename = getTextureNamesFromImage(flipbookTexture.image, texProp.tex_format, model)
            if flipbookProp.exportMode == "Individual":
                imageName = flipbookTexture.name

            # We don't know yet if this already exists, cause we need the full set
            # of images which contribute to the palette, which we don't get until
            # writeTexRefCITextures (in case the other texture in multitexture contributes).
            # So these get created but may get dropped later.
            fImage_temp = FImage(
                imageName,
                texFormatOf[texProp.tex_format],
                texBitSizeF3D[texProp.tex_format],
                flipbookTexture.image.size[0],
                flipbookTexture.image.size[1],
                filename,
            )

            pal = mergePalettes(pal, getColorsUsedInImage(flipbookTexture.image, texProp.ci_format))

            flipbook.textureNames.append(fImage_temp.name)
            flipbook.images.append((flipbookTexture.image, fImage_temp))

        # print(f"Palette length: {len(pal)}") # Checked in moreSetupFromModel
        return allImages, flipbook, pal

    def writeTexRefCITextures(
        self,
        flipbook: Union[TextureFlipbook, None],
        fMaterial: FMaterial,
        imagesSharingPalette: list[bpy.types.Image],
        pal: list[int],
        texFmt: str,
        palFmt: str,
    ):
        if flipbook is None:
            return super().writeTexRefCITextures(None, fMaterial, imagesSharingPalette, pal, texFmt, palFmt)
        model = self.getFlipbookOwner()
        for i in range(len(flipbook.images)):
            image, fImage_temp = flipbook.images[i]
            imageKey = FImageKey(image, texFmt, palFmt, imagesSharingPalette)
            fImage = model.getTextureAndHandleShared(imageKey)
            if fImage is not None:
                flipbook.textureNames[i] = fImage.name
                flipbook.images[i] = (image, fImage)
            else:
                fImage = fImage_temp
                model.addTexture(imageKey, fImage, fMaterial)
            writeCITextureData(image, fImage, pal, palFmt, texFmt)
        # Have to delay this until here because texture names may have changed
        model.addFlipbookWithRepeatCheck(flipbook)

    def processTexRefNonCITextures(self, fMaterial: FMaterial, material: bpy.types.Material, index: int):
        model = self.getFlipbookOwner()
        flipbookProp = getattr(material.flipbookGroup, f"flipbook{index}")
        texProp = getattr(material.f3d_mat, f"tex{index}")
        if not usesFlipbook(material, flipbookProp, index, True, ootFlipbookReferenceIsValid):
            return super().processTexRefNonCITextures(fMaterial, material, index)
        if len(flipbookProp.textures) == 0:
            raise PluginError(f"{str(material)} cannot have a flipbook material with no flipbook textures.")

        flipbook = TextureFlipbook(flipbookProp.name, flipbookProp.exportMode, [], [])
        allImages = self.validateImages(material, index)
        for flipbookTexture in flipbookProp.textures:
            # print(f"Texture: {str(flipbookTexture.image)}")
            # Can't use saveOrGetTextureDefinition because the way it gets the
            # image key and the name from the texture property won't work here.
            imageKey = FImageKey(flipbookTexture.image, texProp.tex_format, texProp.ci_format, [flipbookTexture.image])
            fImage = model.getTextureAndHandleShared(imageKey)
            if fImage is None:
                imageName, filename = getTextureNamesFromImage(flipbookTexture.image, texProp.tex_format, model)
                if flipbookProp.exportMode == "Individual":
                    imageName = flipbookTexture.name
                fImage = FImage(
                    imageName,
                    texFormatOf[texProp.tex_format],
                    texBitSizeF3D[texProp.tex_format],
                    flipbookTexture.image.size[0],
                    flipbookTexture.image.size[1],
                    filename,
                )
                model.addTexture(imageKey, fImage, fMaterial)

            flipbook.textureNames.append(fImage.name)
            flipbook.images.append((flipbookTexture.image, fImage))

        self.addFlipbookWithRepeatCheck(flipbook)
        return allImages, flipbook

    def writeTexRefNonCITextures(self, flipbook: Union[TextureFlipbook, None], texFmt: str):
        if flipbook is None:
            return super().writeTexRefNonCITextures(flipbook, texFmt)
        for image, fImage in flipbook.images:
            writeNonCITextureData(image, fImage, texFmt)

    def onMaterialCommandsBuilt(self, fMaterial, material, drawLayer):
        super().onMaterialCommandsBuilt(fMaterial, material, drawLayer)
        # handle dynamic material calls
        gfxList = fMaterial.material
        matDrawLayer = getattr(material.ootMaterial, drawLayer.lower())
        for i in range(8, 14):
            if getattr(matDrawLayer, "segment" + format(i, "X")):
                gfxList.commands.append(
                    SPDisplayList(GfxList("0x" + format(i, "X") + "000000", GfxListTag.Material, DLFormat.Static))
                )
        for i in range(0, 2):
            p = "customCall" + str(i)
            if getattr(matDrawLayer, p):
                gfxList.commands.append(
                    SPDisplayList(GfxList(getattr(matDrawLayer, p + "_seg"), GfxListTag.Material, DLFormat.Static))
                )

    def onAddMesh(self, fMesh, contextObj):
        if contextObj is not None and hasattr(contextObj, "ootDynamicTransform"):
            if contextObj.ootDynamicTransform.billboard:
                fMesh.draw.commands.append(SPMatrix("0x01000000", "G_MTX_MODELVIEW | G_MTX_NOPUSH | G_MTX_MUL"))


class OOTGfxFormatter(GfxFormatter):
    def __init__(self, scrollMethod):
        GfxFormatter.__init__(self, scrollMethod, 64, None)


class OOTTriangleConverterInfo(TriangleConverterInfo):
    def __init__(self, obj, armature, f3d, transformMatrix, infoDict):
        TriangleConverterInfo.__init__(self, obj, armature, f3d, transformMatrix, infoDict)

    def getMatrixAddrFromGroup(self, groupIndex):
        return format((0x0D << 24) + MTX_SIZE * self.vertexGroupInfo.vertexGroupToMatrixIndex[groupIndex], "#010x")


class OOTVertexGroupInfo(VertexGroupInfo):
    def __init__(self):
        self.vertexGroupToMatrixIndex = {}
        VertexGroupInfo.__init__(self)


# class OOTBox:
# 	def __init__(self):
# 		self.minBounds = [-2**8, -2**8]
# 		self.maxBounds = [2**8 - 1, 2**8 - 1]


class OOTF3DContext(F3DContext):
    def __init__(self, f3d, limbList, basePath):
        self.limbList = limbList
        self.dlList = []  # in the order they are rendered
        self.isBillboard = False
        self.flipbooks = {}  # {(segment, draw layer) : TextureFlipbook}
        self.isAnimSkinLimb = False # easier to read than checking if the data is None
        self.animSkinLimbData = None

        materialContext = createF3DMat(None, preset="oot_shaded_solid")
        # materialContext.f3d_mat.rdp_settings.g_mdsft_cycletype = "G_CYC_1CYCLE"
        F3DContext.__init__(self, f3d, basePath, materialContext)

    def getLimbName(self, index):
        return self.limbList[index]

    def getBoneName(self, index):
        return "bone" + format(index, "03") + "_" + self.getLimbName(index)

    def vertexFormatPatterns(self, data):
        # position, uv, color/normal
        if "VTX" in data:
            return ["VTX\s*\(([^,]*),([^,]*),([^,]*),([^,]*),([^,]*),([^,]*),([^,]*),([^,]*),([^,]*)\)"]
        else:
            return F3DContext.vertexFormatPatterns(self, data)

    # For game specific instance, override this to be able to identify which verts belong to which bone.
    def setCurrentTransform(self, name, flagList="G_MTX_NOPUSH | G_MTX_LOAD | G_MTX_MODELVIEW"):
        if name[:4].lower() == "0x0d":
            # This code is for skeletons
            index = int(int(name[4:], 16) / MTX_SIZE)
            if index < len(self.dlList):
                transformName = self.getLimbName(self.dlList[index].limbIndex)

            # This code is for jabu jabu level, requires not adding to self.dlList?
            else:
                transformName = name
                self.matrixData[name] = mathutils.Matrix.Identity(4)
                print(f"Matrix {name} has not been processed from dlList, substituting identity matrix.")

            F3DContext.setCurrentTransform(self, transformName, flagList)

        else:
            try:
                pointer = hexOrDecInt(name)
            except:
                F3DContext.setCurrentTransform(self, name, flagList)
            else:
                if pointer >> 24 == 0x01:
                    self.isBillboard = True
                else:
                    print("Unhandled matrix: " + name)

    def processDLName(self, name):
        # Commands loaded to 0x0C are material related only.
        try:
            pointer = hexOrDecInt(name)
        except:
            if name == "gEmptyDL":
                return None
            return name
        else:
            segment = pointer >> 24
            if segment >= 0x08 and segment <= 0x0D:
                setattr(self.materialContext.ootMaterial.opaque, "segment" + format(segment, "1X"), True)
                setattr(self.materialContext.ootMaterial.transparent, "segment" + format(segment, "1X"), True)
                self.materialChanged = True
            return None
        return name

    def processVertexDataName(self, name, dlData, num, start):
        try:
            pointer = hexOrDecInt(name)
        except:
            return name
        else:
            if (self.isAnimSkinLimb): # Do Skin Limb stuff
                if ((pointer & 0x00FFFFFF) % 0x10): # Check if the offset makes sense
                    raise PluginError(f"Segment offset not aligned with sizeof(Vtx)")
                else:
                    ootParseAnimatedLimb(self, pointer, num, start, dlData)
            else:
                raise PluginError("Vertex data is in a segment and cannot be parsed") # Someone could add support for assigning a segment to a bone, but that's a really dangerous idea
            return None
        return name

    def processTextureName(self, textureName):
        try:
            pointer = hexOrDecInt(textureName)
        except:
            return textureName
        else:
            return textureName
            # if (pointer >> 24) == 0x08:
            # 	print("Unhandled OOT pointer: " + textureName)

    def getMaterialKey(self, material: bpy.types.Material):
        return (material.ootMaterial.key(), super().getMaterialKey(material))

    def clearGeometry(self):
        self.dlList = []
        self.isBillboard = False
        super().clearGeometry()

    def clearMaterial(self):
        self.isBillboard = False

        # Don't clear ootMaterial, some skeletons (Link) require dynamic material calls to be preserved between limbs
        clearOOTFlipbookProperty(self.materialContext.flipbookGroup.flipbook0)
        clearOOTFlipbookProperty(self.materialContext.flipbookGroup.flipbook1)
        F3DContext.clearMaterial(self)

    def postMaterialChanged(self):
        pass

    def handleTextureReference(
        self,
        name: str,
        image: F3DTextureReference,
        material: bpy.types.Material,
        index: int,
        tileSettings: DPSetTile,
        data: str,
    ):
        # check for texture arrays.
        clearOOTFlipbookProperty(getattr(material.flipbookGroup, "flipbook" + str(index)))
        match = re.search(f"(0x0[0-9a-fA-F])000000", name)
        if match:
            segment = int(match.group(1), 16)
            flipbookKey = (segment, material.f3d_mat.draw_layer.oot)
            if flipbookKey in self.flipbooks:
                flipbook = self.flipbooks[flipbookKey]

                flipbookProp = getattr(material.flipbookGroup, "flipbook" + str(index))
                flipbookProp.enable = True
                flipbookProp.exportMode = flipbook.exportMode
                if flipbookProp.exportMode == "Array":
                    flipbookProp.name = flipbook.name

                if len(flipbook.textureNames) == 0:
                    raise PluginError(
                        f'Texture array "{flipbookProp.name}" pointed at segment {hex(segment)} is a zero element array, which is invalid.'
                    )
                for textureName in flipbook.textureNames:
                    image = self.loadTexture(data, textureName, None, tileSettings, False)
                    if not isinstance(image, bpy.types.Image):
                        raise PluginError(
                            f'Could not find texture "{textureName}", so it can not be used in a flipbook texture.\n'
                            f"For OOT scenes this may be because the scene's draw config references textures not stored in its scene/room files.\n"
                            f"In this case, draw configs that use flipbook textures should only be used for one scene.\n"
                        )
                    flipbookProp.textures.add()
                    flipbookProp.textures[-1].image = image

                    if flipbookProp.exportMode == "Individual":
                        flipbookProp.textures[-1].name = textureName

                texProp = getattr(material.f3d_mat, "tex" + str(index))
                texProp.tex = flipbookProp.textures[0].image  # for visual purposes only, will be ignored
                texProp.use_tex_reference = True
                texProp.tex_reference = name
            else:
                super().handleTextureReference(name, image, material, index, tileSettings, data)
        else:
            super().handleTextureReference(name, image, material, index, tileSettings, data)

    def handleTextureValue(self, material: bpy.types.Material, image: bpy.types.Image, index: int):
        clearOOTFlipbookProperty(getattr(material.flipbookGroup, "flipbook" + str(index)))
        super().handleTextureValue(material, image, index)

    def handleApplyTLUT(
        self,
        material: bpy.types.Material,
        texProp: TextureProperty,
        tlut: bpy.types.Image,
        index: int,
    ):
        flipbook = getattr(material.flipbookGroup, "flipbook" + str(index))
        if usesFlipbook(material, flipbook, index, True, ootFlipbookReferenceIsValid):
            # Don't apply TLUT to texProp.tex, as it is the same texture as the first flipbook texture.
            # Make sure to check if tlut is already applied (ex. LOD skeleton uses same flipbook textures)
            # applyTLUTToIndex() doesn't check for this if texProp.use_tex_reference.
            for flipbookTexture in flipbook.textures:
                if flipbookTexture.image not in self.tlutAppliedTextures:
                    self.applyTLUT(flipbookTexture.image, tlut)
                    self.tlutAppliedTextures.append(flipbookTexture.image)
        else:
            super().handleApplyTLUT(material, texProp, tlut, index)

def clearOOTFlipbookProperty(flipbookProp):
    flipbookProp.enable = False
    flipbookProp.name = "sFlipbookTextures"
    flipbookProp.exportMode = "Array"
    flipbookProp.textures.clear()


# Skin Skeleton classes, could probably be simplified

class SkinVertex:
    def __init__(
        self,
        index: int,
        s: int,
        t: int,
        normX: int,
        normY: int,
        normZ: int,
        alpha: int,
    ):
        self.index = index
        self.s = s
        self.t = t
        self.normX = normX
        self.normY = normY
        self.normZ = normZ
        self.alpha = alpha

class SkinTransformation:
    def __init__(
        self,
        limbIndex: int,
        x: int,
        y: int,
        z: int,
        scale: int,
    ):
        self.limbIndex = limbIndex
        self.x = x
        self.y = y
        self.z = z
        self.scale = scale

class SkinLimbModif:
    def __init__(
        self,
        vtxCount: int = 0,
        transformCount: int = 0,
        unk_4: int = 0,
        skinVertices: list[SkinVertex] = [],
        limbTransformations: list[SkinTransformation] = [],
    ):
        self.vtxCount = vtxCount
        self.transformCount = transformCount
        self.unk_4 = unk_4
        self.skinVertices = skinVertices
        self.limbTransformations = limbTransformations  

    def populateSkinLimbModif(self, dlData, skinVertices, limbTransformations):
        # vertices
        self.vtxCount = 0
        vtxArrayString = captureData(dlData, skinVertices, "Struct_800A57C0", False) # SkinVertex
        vtxData = []
        for vtxMatch in re.finditer("\{\s*([^,\s]*)\s*,\s*([^,\s]*)\s*,\s*([^,\s]*)\s*,\s*([^,\s]*)\s*,\s*([^,\s]*)\s*,\s*([^,\s]*)\s*,\s*([^,\s]*)[\s,]*},", vtxArrayString):
            self.vtxCount += 1 #self.vtxCount = ARRAY_COUNTU(skinVertices)
            vtxData.append(SkinVertex(
                hexOrDecInt(vtxMatch.group(1)), hexOrDecInt(vtxMatch.group(2)), hexOrDecInt(vtxMatch.group(3)), 
                hexOrDecInt(vtxMatch.group(4)), hexOrDecInt(vtxMatch.group(5)), hexOrDecInt(vtxMatch.group(6)), 
                hexOrDecInt(vtxMatch.group(7)),
                ))

        self.skinVertices = vtxData

        # limb transformations
        self.transformCount = 0
        limbTransformArrayString = captureData(dlData, limbTransformations, "Struct_800A598C_2", False) # SkinTransformation
        transformData = []
        for transformMatch in re.finditer("\{\s*([^,\s]*)\s*,\s*([^,\s]*)\s*,\s*([^,\s]*)\s*,\s*([^,\s]*)\s*,\s*([^,\s]*)[\s,]*},", limbTransformArrayString):
            self.transformCount += 1 #self.transformCount = ARRAY_COUNTU(limbTransformations)
            transformData.append(SkinTransformation(
                hexOrDecInt(transformMatch.group(1)), hexOrDecInt(transformMatch.group(2)), hexOrDecInt(transformMatch.group(3)), 
                hexOrDecInt(transformMatch.group(4)), hexOrDecInt(transformMatch.group(5)),
                ))
        self.limbTransformations = transformData

class SkinAnimatedLimbData:
    def __init__(
        self,
        totalVtxCount: int = 0,
        limbModifCount: int = 0,
        limbModifications: list[SkinLimbModif] = [],
        dList: str = "",
    ):
        self.totalVtxCount = totalVtxCount
        self.limbModifCount = limbModifCount
        self.limbModifications = limbModifications
        self.dList = dList

    def populateLimbModifications(self, dlData, arrayName, continueOnError): # 135
        arrayString = captureData(dlData, arrayName, "Struct_800A598C", False)
        
        self.limbModifCount = 0
        for match in re.finditer("\{\s*([^,\s]*)\s*,\s*([^,\s]*)\s*,\s*([^,\s]*)\s*,\s*([^,\s]*)\s*,\s*([^,\s]*)[\s,]*},", arrayString):
            curModif = SkinLimbModif(unk_4=hexOrDecInt(match.group(3)))
            curModif.populateSkinLimbModif(dlData, match.group(4), match.group(5))
            self.limbModifications.append(curModif)
            self.limbModifCount += 1 #ARRAY_COUNT(arrayName)

class SkinF3DVert(F3DVert):
    def __init__(
        self,
        position: Vector,
        uv: Vector,
        rgb: Optional[Vector],
        normal: Optional[Vector],
        alpha: float,
        weights: list[VertexWeight], # not optional for this
        modif: SkinLimbModif,
    ):
        F3DVert.__init__(self, position, uv, rgb, normal, alpha, weights)
        self.modif = modif

# Skin Skeleton functions 
    
# Strange function that pretty much just takes a limb matrix and undoes the blender scale to create an OoT matrix
def ootRetrieveMatrixData(f3dContext: OOTF3DContext, limbIndex: int):
    matrixName = f3dContext.getLimbName(limbIndex)
    if matrixName in f3dContext.matrixData:
        blenderMatrix = f3dContext.matrixData[matrixName]
        matrixScale = blenderMatrix.to_scale()
        matrixTranslation = blenderMatrix.to_translation()
        ootMatrix = Matrix.Translation(Vector((matrixTranslation.x / matrixScale.x, matrixTranslation.y / matrixScale.y, matrixTranslation.z / matrixScale.z))) @ blenderMatrix.to_euler().to_matrix().to_4x4()
        return ootMatrix
    else:
        print(f3dContext.matrixData)
        raise PluginError("Transform matrix not specified for " + matrixName)

def ootParseAnimatedLimb(f3dContext: OOTF3DContext, pointer: int, num: int, start: int, dlData):
    # Name the vertex data after the segment it is referencing (ex. Segment8VtxData)
    vtxDataName = f"Segment{pointer >> 24}VtxData"

    # Create vertices if not already existing
    if vtxDataName not in f3dContext.vertexData:
        print(f"Creating object for SkinAnimatedLimbData")
        # Create "skinAnimLimbData" object representing the original SkinAnimatedLimbData struct
        skinAnimLimbData = SkinAnimatedLimbData(totalVtxCount=hexOrDecInt(f3dContext.animSkinLimbData.group(1)),dList=f3dContext.animSkinLimbData.group(4))
        # Populate skinAnimLimbData with its items
        skinAnimLimbData.populateLimbModifications(dlData, f3dContext.animSkinLimbData.group(3), False)
        # skinAnimLimbData is now a functional copy of the original SkinAnimatedLimbData data

        vertexData = [None] * skinAnimLimbData.totalVtxCount

        # This very strange implementation mimics how OoT transforms its vertices so that there won't be any potential mistakes.
        # It also doesn't assume the rotation of each bone is 0.
        # See Skin_ApplyLimbModifications
        for modif in skinAnimLimbData.limbModifications:
            transformCount = modif.transformCount
            skinVertices = modif.skinVertices
            limbTransformations = modif.limbTransformations
            vtxPoint = Vector((0, 0, 0))
            weightData = []

            if transformCount == 1:
                transformEntry = limbTransformations[0]
                matrix = ootRetrieveMatrixData(f3dContext, transformEntry.limbIndex)
                vtxPoint = (matrix @ Matrix.Translation(Vector((transformEntry.x, transformEntry.y, transformEntry.z)))).to_translation()
            # OoT has an optional argument for this. I don't know what it's for, so for now it's unconditionally False, but could be reactivated if a purpose was found
            elif False:
                transformEntry = limbTransformations[modif.unk_4]
                matrix = ootRetrieveMatrixData(f3dContext, transformEntry.limbIndex)
                vtxPoint = (matrix @ Matrix.Translation(Vector((transformEntry.x, transformEntry.y, transformEntry.z)))).to_translation()
            else:
                for transformEntry in limbTransformations:
                    scale = transformEntry.scale * 0.01
                    matrix = ootRetrieveMatrixData(f3dContext, transformEntry.limbIndex)
                    vtxPoint += ((matrix @ Matrix.Translation(Vector((transformEntry.x, transformEntry.y, transformEntry.z)))).to_translation()) * scale           
     
            for transformEntry in limbTransformations:
                weightData.append(VertexWeight(transformEntry.limbIndex, transformEntry.scale * 0.01))
            
            for skinVertex in skinVertices:
                vertexData[skinVertex.index] = SkinF3DVert(
                    Vector((vtxPoint.x, vtxPoint.y, vtxPoint.z)),
                    Vector((skinVertex.s, skinVertex.t)),
                    Vector((1, 1, 1)), # why is it labelled optional if it's not optional
                    Vector((skinVertex.normX, skinVertex.normY, skinVertex.normZ)),
                    skinVertex.alpha,
                    weightData,
                    modif,
                )

        f3dContext.vertexData[vtxDataName] = vertexData

    ootAddSkinVertexData(f3dContext, num, start, vtxDataName, int((pointer & 0x00FFFFFF) / 0x10))


def ootAddSkinVertexData(f3dContext: OOTF3DContext, num, start, vertexDataName, vertexDataOffset):
    vertexData = f3dContext.vertexData[vertexDataName]

    # TODO: material index not important?
    count = math_eval(num, f3dContext.f3d)
    start = math_eval(start, f3dContext.f3d)

    if start + count > len(f3dContext.vertexBuffer):
        raise PluginError(
            "Vertex buffer of size "
            + str(len(f3dContext.vertexBuffer))
            + " too small, attempting load into "
            + str(start)
            + ", "
            + str(start + count)
        )
    if vertexDataOffset + count > len(vertexData):
        raise PluginError(
            f"Attempted to read vertex data out of bounds.\n"
            f"{vertexDataName} is of size {len(vertexData)}, "
            f"attemped read from ({vertexDataOffset}, {vertexDataOffset + count})"
        )
    for i in range(count):
        modif = vertexData[vertexDataOffset + i].modif
        matrixLimbIndex = modif.limbTransformations[modif.unk_4].limbIndex
        #if ((vertexDataOffset + i) == 99 or (vertexDataOffset + i) == 148):
        f3dContext.vertexBuffer[start + i] = BufferVertex(vertexData[vertexDataOffset + i], f3dContext.getLimbName(0), 0) # Constructor takes both int and string, but only strings work