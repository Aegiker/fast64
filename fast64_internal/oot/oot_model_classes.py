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
    vertexCount = 0
    testData = [
        Vector((2618.599854, 624.440002, -409.050018)), #0
        Vector((1440.550049, -534.650024, -267.000000)), #1
        Vector((1427.199951, 495.000000, -331.599976)), #2
        Vector((1440.550049, -534.650024, -267.000000)), #3
        Vector((255.999985, -533.000000, -692.400024)), #4
        Vector((1427.199951, 495.000000, -331.599976)), #5
        Vector((1228.199951, -1199.000000, 718.999939)), #6
        Vector((-456.399963, -746.699951, -435.599976)), #7
        Vector((1440.550049, -534.650024, -267.000000)), #8
        Vector((255.999985, -533.000000, -692.400024)), #9
        Vector((-646.000000, -1207.299927, 316.950043)), #10
        Vector((-456.399963, -746.699951, -435.599976)), #11
        Vector((1228.199951, -1199.000000, 718.999939)), #12
        Vector((1440.550049, -534.650024, -267.000000)), #13
        Vector((3472.199707, -660.799988, 47.999996)), #14
        Vector((1228.199951, -1199.000000, 718.999939)), #15
        Vector((3472.199707, -660.799988, 47.999996)), #16
        Vector((3367.799805, -1005.999939, 707.999939)), #17
        Vector((1228.199951, -1199.000000, 718.999939)), #18
        Vector((2618.599854, 624.440002, -409.050018)), #19
        Vector((-646.000000, -1207.299927, 316.950043)), #20
        Vector((-1314.099854, -307.700012, 69.400009)), #21
        Vector((-456.399963, -746.699951, -435.599976)), #22
        Vector((-1314.099854, -307.700012, 69.400009)), #23
        Vector((-646.000000, -1207.299927, 316.950043)), #24
        Vector((-1525.899902, -704.399963, 316.100006)), #25
        Vector((-1051.999878, -307.700012, 770.599915)), #26
        Vector((67.599991, -746.699951, 787.799927)), #27
        Vector((-1051.999878, -307.700012, 770.599915)), #28
        Vector((3470.599609, -663.999939, 1305.999878)), #29
        Vector((1440.550049, -534.650024, 1655.000000)), #30
        Vector((2594.410156, 620.710022, 1788.000000)), #31
        Vector((1690.199951, 496.399963, 1199.599976)), #32
        Vector((1440.550049, -534.650024, 1655.000000)), #33
        Vector((2594.410156, 620.710022, 1788.000000)), #34
        Vector((1690.199951, 496.399963, 1199.599976)), #35
        Vector((1304.000000, -533.000000, -2.599998)), #36
        Vector((1440.550049, -534.650024, 1655.000000)), #37
        Vector((67.599991, -746.699951, 787.799927)), #38
        Vector((1440.550049, -534.650024, 1655.000000)), #39
        Vector((1304.000000, -533.000000, -2.599998)), #40
        Vector((1228.199951, -1199.000000, 718.999939)), #41
        Vector((1228.199951, -1199.000000, 718.999939)), #42
        Vector((67.599991, -746.699951, 787.799927)), #43
        Vector((-646.000000, -1207.299927, 316.950043)), #44
        Vector((1228.199951, -1199.000000, 718.999939)), #45
        Vector((3367.799805, -1005.999939, 707.999939)), #46
        Vector((3470.599609, -663.999939, 1305.999878)), #47
        Vector((3869.899902, 1335.599976, -418.999969)), #48
        Vector((2618.599854, 624.440002, -409.050018)), #49
        Vector((1427.199951, 495.000000, -331.599976)), #50
        Vector((2899.410156, 63.179993, -356.000000)), #51
        Vector((2618.599854, 624.440002, -409.050018)), #52
        Vector((3525.000000, 526.799988, -663.999939)), #53
        Vector((3652.239990, 132.099991, -159.999985)), #54
        Vector((4221.000000, 1009.000000, 715.000000)), #55
        Vector((4165.399902, 39.799999, -81.000000)), #56
        Vector((4231.750000, 1304.000000, 708.000000)), #57
        Vector((3919.969727, 301.700012, -202.949982)), #58
        Vector((1441.299927, -178.099976, -227.599960)), #59
        Vector((1690.199951, 496.399963, 1199.599976)), #60
        Vector((1149.299927, 546.499878, 187.799973)), #61
        Vector((1304.000000, -533.000000, -2.599998)), #62
        Vector((2338.599854, 1725.799927, 1012.999939)), #63
        Vector((2337.399902, 1727.199951, 361.999969)), #64
        Vector((1427.199951, 495.000000, -331.599976)), #65
        Vector((523.599976, -177.399979, -180.599976)), #66
        Vector((255.999985, -533.000000, -692.400024)), #67
        Vector((3472.199707, -660.799988, 47.999996)), #68
        Vector((4118.099609, 1475.299927, 708.000000)), #69
        Vector((4648.199707, 1059.399902, 203.000000)), #70
        Vector((2337.399902, 1727.199951, 361.999969)), #71
        Vector((2337.399902, 1727.199951, 361.999969)), #72
        Vector((2338.599854, 1725.799927, 1012.999939)), #73
        Vector((3526.199951, 1668.949951, 709.999939)), #74
        Vector((4648.199707, 1060.299927, 1221.000000)), #75
        Vector((3869.899902, 1335.599976, 1802.000000)), #76
        Vector((3919.969727, 302.169983, 1579.050049)), #77
        Vector((3652.239990, 132.099991, 1533.000000)), #78
        Vector((3526.199707, 527.599976, 2098.000000)), #79
        Vector((2594.410156, 620.710022, 1788.000000)), #80
        Vector((2901.299805, 51.399994, 1763.000000)), #81
        Vector((4168.199707, 36.600002, 1497.000000)), #82
        Vector((4221.000000, 1009.000000, 715.000000)), #83
        Vector((3652.239990, 132.099991, 1533.000000)), #84
        Vector((2338.599854, 1725.799927, 1012.999939)), #85
        Vector((1690.199951, 496.399963, 1199.599976)), #86
        Vector((3869.899902, 1335.599976, 1802.000000)), #87
        Vector((3470.599609, -663.999939, 1305.999878)), #88
        Vector((2594.410156, 620.710022, 1788.000000)), #89
        Vector((737.199951, 685.199951, -71.500008)), #90
        Vector((1149.299927, 546.499878, 187.799973)), #91
        Vector((523.599976, -177.399979, -180.599976)), #92
        Vector((4231.750000, 1304.000000, 708.000000)), #93
        Vector((2337.399902, 1727.199951, 361.999969)), #94
        Vector((3526.199951, 1668.949951, 709.999939)), #95
        Vector((4648.199707, 1059.399902, 203.000000)), #96
        Vector((1441.299927, -178.099976, -227.599960)), #97
        Vector((3919.969727, 302.169983, 1579.050049)), #98
        Vector((3992.299805, -1078.300049, 711.000000)), #99
        Vector((5365.000000, -746.500000, 424.000000)), #100
        Vector((4166.899902, -1124.099854, 716.000000)), #101
        Vector((6954.000000, -157.000000, 904.000000)), #102
        Vector((5366.000000, -745.500000, 990.000000)), #103
        Vector((5426.899414, -1124.499878, 1100.000000)), #104
        Vector((5775.599609, -946.799927, 698.999939)), #105
        Vector((5426.899414, -1124.499878, 1100.000000)), #106
        Vector((5366.000000, -745.500000, 990.000000)), #107
        Vector((6375.599609, -471.199951, 707.000000)), #108
        Vector((6954.000000, -156.000000, 511.000000)), #109
        Vector((6954.000000, -157.000000, 904.000000)), #110
        Vector((6375.599609, -471.199951, 707.000000)), #111
        Vector((6375.599609, -471.199951, 707.000000)), #112
        Vector((5426.899414, -1125.000000, 313.999969)), #113
        Vector((6954.000000, -156.000000, 511.000000)), #114
        Vector((5775.599609, -946.799927, 698.999939)), #115
        Vector((5426.899414, -1125.000000, 313.999969)), #116
        Vector((6375.599609, -471.199951, 707.000000)), #117
        Vector((5426.899414, -1125.000000, 313.999969)), #118
        Vector((5365.000000, -746.500000, 424.000000)), #119
        Vector((6954.000000, -156.000000, 511.000000)), #120
        Vector((5365.000000, -746.500000, 424.000000)), #121
        Vector((3472.199707, -660.799988, 47.999996)), #122
        Vector((3367.799805, -1005.999939, 707.999939)), #123
        Vector((3470.599609, -663.999939, 1305.999878)), #124
        Vector((5366.000000, -745.500000, 990.000000)), #125
        Vector((6123.000000, 125.000000, 1193.000000)), #126
        Vector((5298.200195, 57.399998, 1338.000000)), #127
        Vector((6954.000000, 550.000000, 711.000000)), #128
        Vector((6123.000000, 125.000000, 1193.000000)), #129
        Vector((5365.000000, -746.500000, 424.000000)), #130
        Vector((5298.200195, 57.399998, 89.000000)), #131
        Vector((6115.000000, 125.000000, 229.000000)), #132
        Vector((6954.000000, -156.000000, 511.000000)), #133
        Vector((6115.000000, 125.000000, 229.000000)), #134
        Vector((6954.000000, 550.000000, 711.000000)), #135
        Vector((3470.599609, -663.999939, 1305.999878)), #136
        Vector((5366.000000, -745.500000, 990.000000)), #137
        Vector((5298.200195, 57.399998, 1338.000000)), #138
        Vector((5298.200195, 57.399998, 89.000000)), #139
        Vector((5365.000000, -746.500000, 424.000000)), #140
        Vector((3472.199707, -660.799988, 47.999996)), #141
        Vector((5366.000000, -745.500000, 990.000000)), #142
        Vector((6954.000000, -157.000000, 904.000000)), #143
        Vector((6123.000000, 125.000000, 1193.000000)), #144
        Vector((6954.000000, -156.000000, 511.000000)), #145
        Vector((4166.899902, -1124.099854, 716.000000)), #146
        Vector((5775.599609, -946.799927, 698.999939)), #147
        Vector((3992.299805, -1078.300049, 711.000000)), #148
        Vector((3612.000000, 687.000000, -1341.000000)), #149
        Vector((1252.500000, 591.000000, -1156.000000)), #150
        Vector((2669.399902, 132.899994, -1105.000000)), #151
        Vector((1252.500000, 591.000000, -1527.000000)), #152
        Vector((2669.399902, 132.899994, -1576.999878)), #153
        Vector((1252.500000, 591.000000, -1527.000000)), #154
        Vector((1252.500000, 591.000000, -1156.000000)), #155
        Vector((3612.000000, 687.000000, -1341.000000)), #156
        Vector((1186.500000, 287.500000, -1341.000000)), #157
        Vector((1186.500000, 287.500000, -1341.000000)), #158
        Vector((2669.399902, 132.899994, -1576.999878)), #159
        Vector((2669.399902, 132.899994, -1105.000000)), #160
        Vector((3612.000000, 687.000000, -1341.000000)), #161
        Vector((9.000000, 505.000000, -1339.000000)), #162
        Vector((1252.500000, 591.000000, -1156.000000)), #163
        Vector((1252.500000, 591.000000, -1527.000000)), #164
        Vector((1186.500000, 287.500000, -1341.000000)), #165
        Vector((-1314.099854, -307.700012, 69.400009)), #166
        Vector((-1525.899902, -704.399963, 316.100006)), #167
        Vector((-1613.099854, 219.599991, 314.400024)), #168
        Vector((-1051.999878, -307.700012, 770.599915)), #169
        Vector((3526.199707, 527.599976, 2098.000000)), #170
        Vector((3919.969727, 302.169983, 1579.050049)), #171
        Vector((4360.839844, 277.160004, 1844.000000)), #172
        Vector((3869.899902, 1335.599976, 1802.000000)), #173
        Vector((4428.319824, 947.320007, 1739.000000)), #174
        Vector((4648.199707, 1060.299927, 1221.000000)), #175
        Vector((4715.600098, 144.360001, 1198.999878)), #176
        Vector((5727.000000, 257.000000, 1593.000000)), #177
        Vector((2594.410156, 620.710022, 1788.000000)), #178
        Vector((5726.000000, 256.000000, -180.000000)), #179
        Vector((4714.640137, 141.359985, 213.000000)), #180
        Vector((4360.839844, 277.119995, -360.000000)), #181
        Vector((5725.000000, 255.000000, 107.000000)), #182
        Vector((6029.500000, 102.500000, 1707.000000)), #183
        Vector((6030.500000, 100.500000, 1157.000000)), #184
        Vector((6369.000000, 374.000000, 1455.000000)), #185
        Vector((5726.000000, 256.000000, 1307.000000)), #186
        Vector((6030.500000, 99.500000, 257.000000)), #187
        Vector((6157.000000, 631.000000, 166.000000)), #188
        Vector((4428.359863, 947.320007, -325.000000)), #189
        Vector((3525.000000, 526.799988, -663.999939)), #190
        Vector((3869.899902, 1335.599976, -418.999969)), #191
        Vector((6156.000000, 634.000000, 1646.000000)), #192
        Vector((6156.000000, 635.000000, 1277.000000)), #193
        Vector((3919.969727, 301.700012, -202.949982)), #194
        Vector((6369.000000, 373.000000, -41.000000)), #195
        Vector((2618.599854, 624.440002, -409.050018)), #196
        Vector((4648.199707, 1059.399902, 203.000000)), #197
        Vector((6029.500000, 102.000000, -341.000000)), #198
        Vector((6156.000000, 634.000000, -249.000000)), #199
        Vector((6369.000000, 374.000000, 1455.000000)), #200
        Vector((6156.000000, 635.000000, 1277.000000)), #201
        Vector((6156.000000, 634.000000, 1646.000000)), #202
        Vector((-194.200012, 1049.599976, -74.800003)), #203
        Vector((2115.399902, 765.999939, -964.999878)), #204
        Vector((737.199951, 685.199951, -71.500008)), #205
        Vector((2254.699951, -413.799988, -915.000000)), #206
        Vector((1441.299927, -178.099976, -227.599960)), #207
        Vector((2495.999756, 52.399998, -864.999939)), #208
        Vector((2455.500000, -474.299988, -727.000000)), #209
        Vector((2074.500000, -101.799995, -842.999939)), #210
        Vector((2455.500000, -474.299988, -403.000000)), #211
        Vector((2493.599854, 53.799995, -284.999969)), #212
        Vector((2074.799805, -101.799995, -413.000000)), #213
        Vector((944.699951, -413.799988, -277.000000)), #214
        Vector((801.799927, 768.799927, -240.999985)), #215
        Vector((-980.200012, 1049.599976, -74.799995)), #216
        Vector((523.599976, -177.399979, -180.599976)), #217
        Vector((1476.099854, -653.899963, -364.999969)), #218
        Vector((2786.099854, -653.899963, -827.999939)), #219
        Vector((3384.799805, -101.799995, -780.000000)), #220
        Vector((1889.799805, -394.000000, -180.999985)), #221
        Vector((3765.500000, -474.299988, -789.000000)), #222
        Vector((-1409.999878, 905.400024, 447.399963)), #223
        Vector((-1613.099854, 219.599991, 314.400024)), #224
        Vector((67.599991, -746.699951, 787.799927)), #225
        Vector((257.099976, -513.900024, -100.199989)), #226
        Vector((-1051.999878, -307.700012, 770.599915)), #227
        Vector((727.299988, 403.899963, -1198.199951)), #228
        Vector((579.799988, -394.000000, -1010.999939)), #229
        Vector((-294.399994, -533.299988, -1191.000000)), #230
        Vector((-1357.699951, 249.499969, -204.500000)), #231
        Vector((3803.599609, 53.799995, -907.000000)), #232
        Vector((-1314.099854, -307.700012, 69.400009)), #233
        Vector((255.999985, -533.000000, -692.400024)), #234
        Vector((-1357.699951, 249.499969, -204.500000)), #235
        Vector((272.599976, 1015.799927, -787.199951)), #236
        Vector((-1078.999878, 309.199982, -752.199951)), #237
        Vector((-921.899963, -513.900024, -831.199890)), #238
        Vector((-294.399994, -533.299988, -1191.000000)), #239
        Vector((-980.200012, 1049.599976, -74.799995)), #240
        Vector((803.199951, 767.399902, -821.000000)), #241
        Vector((3384.500000, -101.799995, -349.000000)), #242
        Vector((2113.199951, 767.399902, -371.999969)), #243
        Vector((1906.299927, 403.899963, 266.699982)), #244
        Vector((801.799927, 768.799927, -240.999985)), #245
        Vector((2751.999756, -506.899994, -301.000000)), #246
        Vector((2074.799805, -101.799995, -413.000000)), #247
        Vector((-456.399963, -746.699951, -435.599976)), #248
        Vector((255.999985, -533.000000, -692.400024)), #249
        Vector((2493.599854, 53.799995, -284.999969)), #250
        Vector((2455.500000, -474.299988, -403.000000)), #251
        Vector((2957.000000, -391.000000, -366.000000)), #252
        Vector((2074.500000, -101.799995, -842.999939)), #253
        Vector((3803.599609, 53.799995, -907.000000)), #254
        Vector((3384.799805, -101.799995, -780.000000)), #255
        Vector((-1314.099854, -307.700012, 69.400009)), #256
        Vector((1442.000000, -506.899994, -891.000000)), #257
        Vector((1476.099854, -653.899963, -364.999969)), #258
        Vector((579.799988, -394.000000, -1010.999939)), #259
        Vector((2455.500000, -474.299988, -727.000000)), #260
        Vector((4233.000000, -59.000000, -636.000000)), #261
        Vector((4267.000000, -393.000000, -393.000000)), #262
        Vector((4267.000000, -393.000000, -819.000000)), #263
        Vector((727.299988, 403.899963, -1198.199951)), #264
        Vector((-702.699951, 249.499969, 315.500000)), #265
        Vector((-194.200012, 1049.599976, -74.800003)), #266
        Vector((-1613.099854, 219.599991, 314.400024)), #267
        Vector((2455.500000, -474.299988, -403.000000)), #268
        Vector((2957.000000, -391.000000, -782.000000)), #269
        Vector((2957.000000, -391.000000, -366.000000)), #270
        Vector((2455.500000, -474.299988, -727.000000)), #271
        Vector((2923.000000, -59.000000, -570.000000)), #272
        Vector((2493.599854, 53.799995, -284.999969)), #273
        Vector((-1078.999878, 309.199982, -752.199951)), #274
        Vector((272.599976, 1015.799927, -787.199951)), #275
        Vector((727.299988, 403.899963, -1198.199951)), #276
        Vector((-294.399994, -533.299988, -1191.000000)), #277
        Vector((1906.299927, 403.899963, 266.699982)), #278
        Vector((2113.199951, 767.399902, -371.999969)), #279
        Vector((1561.999878, 970.099915, -82.299988)), #280
        Vector((2495.999756, 52.399998, -864.999939)), #281
        Vector((2074.500000, -101.799995, -842.999939)), #282
        Vector((2074.799805, -101.799995, -413.000000)), #283
        Vector((-1051.999878, -307.700012, 770.599915)), #284
        Vector((257.099976, -513.900024, -100.199989)), #285
        Vector((-555.000000, 309.199982, 1123.800049)), #286
        Vector((1015.599976, -533.299988, -1.000000)), #287
        Vector((3384.500000, -101.799995, -349.000000)), #288
        Vector((3805.999512, 52.399998, -326.999969)), #289
        Vector((3803.599609, 53.799995, -907.000000)), #290
        Vector((2115.399902, 765.999939, -964.999878)), #291
        Vector((2786.099854, -653.899963, -827.999939)), #292
        Vector((2751.999756, -506.899994, -301.000000)), #293
        Vector((1889.799805, -394.000000, -180.999985)), #294
        Vector((3384.799805, -101.799995, -780.000000)), #295
        Vector((1304.000000, -533.000000, -2.599998)), #296
        Vector((1015.599976, -533.299988, -1.000000)), #297
        Vector((67.599991, -746.699951, 787.799927)), #298
        Vector((3765.500000, -474.299988, -789.000000)), #299
        Vector((3803.599609, 53.799995, -907.000000)), #300
        Vector((4267.000000, -393.000000, -819.000000)), #301
        Vector((-1051.999878, -307.700012, 770.599915)), #302
        Vector((-702.699951, 249.499969, 315.500000)), #303
        Vector((-1613.099854, 219.599991, 314.400024)), #304
        Vector((3384.500000, -101.799995, -349.000000)), #305
        Vector((3384.799805, -101.799995, -780.000000)), #306
        Vector((2113.199951, 767.399902, -371.999969)), #307
        Vector((1441.299927, -178.099976, -227.599960)), #308
        Vector((1889.799805, -394.000000, -180.999985)), #309
        Vector((2923.000000, -59.000000, -570.000000)), #310
        Vector((2957.000000, -391.000000, -366.000000)), #311
        Vector((2957.000000, -391.000000, -782.000000)), #312
        Vector((2495.999756, 52.399998, -864.999939)), #313
        Vector((2074.500000, -101.799995, -842.999939)), #314
        Vector((1442.000000, -506.899994, -891.000000)), #315
        Vector((727.299988, 403.899963, -1198.199951)), #316
        Vector((803.199951, 767.399902, -821.000000)), #317
        Vector((579.799988, -394.000000, -1010.999939)), #318
        Vector((3765.500000, -474.299988, -465.000000)), #319
        Vector((2751.999756, -506.899994, -301.000000)), #320
        Vector((4233.000000, -59.000000, -636.000000)), #321
        Vector((2786.099854, -653.899963, -827.999939)), #322
        Vector((4267.000000, -393.000000, -393.000000)), #323
        Vector((255.999985, -533.000000, -692.400024)), #324
        Vector((-294.399994, -533.299988, -1191.000000)), #325
        Vector((1906.299927, 403.899963, 266.699982)), #326
        Vector((257.099976, -513.900024, -100.199989)), #327
        Vector((1015.599976, -533.299988, -1.000000)), #328
        Vector((-555.000000, 309.199982, 1123.800049)), #329
        Vector((67.599991, -746.699951, 787.799927)), #330
        Vector((-702.699951, 249.499969, 315.500000)), #331
        Vector((4233.000000, -59.000000, -636.000000)), #332
        Vector((3805.999512, 52.399998, -326.999969)), #333
        Vector((4267.000000, -393.000000, -393.000000)), #334
        Vector((3803.599609, 53.799995, -907.000000)), #335
        Vector((3765.500000, -474.299988, -465.000000)), #336
        Vector((3384.500000, -101.799995, -349.000000)), #337
        Vector((1441.299927, -178.099976, -227.599960)), #338
        Vector((2254.699951, -413.799988, -915.000000)), #339
        Vector((1889.799805, -394.000000, -180.999985)), #340
        Vector((737.199951, 685.199951, -71.500008)), #341
        Vector((2115.399902, 765.999939, -964.999878)), #342
        Vector((4168.199707, 36.600002, 1497.000000)), #343
        Vector((5298.200195, 57.399998, 1338.000000)), #344
        Vector((5178.500000, 675.500000, 691.000000)), #345
        Vector((5298.200195, 57.399998, 89.000000)), #346
        Vector((5178.500000, 675.500000, 691.000000)), #347
        Vector((5920.000000, 792.000000, 714.000000)), #348
        Vector((6123.000000, 125.000000, 1193.000000)), #349
        Vector((5920.000000, 792.000000, 714.000000)), #350
        Vector((6954.000000, 550.000000, 711.000000)), #351
        Vector((3472.199707, -660.799988, 47.999996)), #352
        Vector((4165.399902, 39.799999, -81.000000)), #353
        Vector((6115.000000, 125.000000, 229.000000)), #354
        Vector((3470.599609, -663.999939, 1305.999878)), #355
        Vector((6115.000000, 125.000000, 229.000000)), #356
        Vector((4221.000000, 1009.000000, 715.000000)), #357
        Vector((4221.000000, 1009.000000, 715.000000)), #358
        Vector((3984.000000, 1444.199951, 707.999939)), #359
        Vector((4118.099609, 1475.299927, 708.000000)), #360
        Vector((3919.969727, 301.700012, -202.949982)), #361
        Vector((3919.969727, 302.169983, 1579.050049)), #362
        Vector((4118.099609, 1475.299927, 708.000000)), #363
        Vector((3984.000000, 1444.199951, 707.999939)), #364
        Vector((4057.399658, 1364.000000, 691.999939)), #365
        Vector((4715.600098, 144.360001, 1198.999878)), #366
        Vector((4714.640137, 141.359985, 213.000000)), #367
        Vector((4057.399658, 1364.000000, 691.999939)), #368
        Vector((3526.199951, 1668.949951, 709.999939)), #369
        Vector((4648.199707, 1060.299927, 1221.000000)), #370
        Vector((4715.600098, 144.360001, 1198.999878)), #371
        Vector((3526.199951, 1668.949951, 709.999939)), #372
        Vector((4648.199707, 1059.399902, 203.000000)), #373
        Vector((4648.199707, 1059.399902, 203.000000)), #374
    ]

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
        for modif in skinAnimLimbData.limbModifications:
            transformCount = modif.transformCount
            skinVertices = modif.skinVertices
            limbTransformations = modif.limbTransformations
            vtxPoint = Vector((0, 0, 0))
            weightData = []

            if transformCount == 1:
                transformEntry = limbTransformations[modif.unk_4]
                scale = transformEntry.scale * 0.01
                matrix = ootRetrieveMatrixData(f3dContext, transformEntry.limbIndex) # limbTransformations[modif.unk_4].limbIndex
                transformedPosition = (matrix @ Matrix.Translation(Vector((transformEntry.x, transformEntry.y, transformEntry.z)))).to_translation()
                vtxPoint = (transformedPosition * scale)
            # OoT has an optional argument used by the draw call for this condition, but because there is no draw function, it is currently always true. Maybe there could be a checkbox?
            # Epona has this always true except when stateFlags & ENHORSE_JUMPING
            elif False:
                transformationEntry = limbTransformations[modif.unk_4]
                vtxPoint = Vector((transformationEntry.x, transformationEntry.y, transformationEntry.z))
            else:
                for transformEntry in limbTransformations:
                    scale = transformEntry.scale * 0.01
                    matrix = ootRetrieveMatrixData(f3dContext, transformEntry.limbIndex) # limbTransformations[modif.unk_4].limbIndex
                    transformedPosition = (matrix @ Matrix.Translation(Vector((transformEntry.x, transformEntry.y, transformEntry.z)))).to_translation()
                    vtxPoint += (transformedPosition * scale)
                    
     
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
                vertexCount += 1

                #position: Vector,
                #uv: Vector,
                #rgb: Optional[Vector],
                #normal: Optional[Vector],
                #alpha: float,
                #weight: float = 1.0,
            print(f"\n\nVERTEX COUNT: {vertexCount}\n\n")

        f3dContext.vertexData[vtxDataName] = vertexData

        

        for vertexNumber, vertex in enumerate(vertexData):
            print(f"Vertex {vertexNumber}: X:{vertex.position.x} Y:{vertex.position.y} Z:{vertex.position.z}")


            

            

        


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