import os
import os.path
import unittest
import gc
# from matplotlib.pyplot import get
import vtk, qt, ctk, slicer
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
import slicer.modules
from sys import platform
import logging
import time
import numpy as np
import torch

import monai
from monai.inferers.utils import sliding_window_inference
from monai.networks.layers import Norm
from monai.networks.nets.unet import UNet
from monai.transforms import (AddChanneld, Compose, Orientationd, ScaleIntensityRanged, Spacingd, ToTensord, Resized,
                              Resize, CropForegroundd, ScaleIntensityRange)
from monai.transforms.compose import MapTransform
from monai.transforms.post.array import AsDiscrete, KeepLargestConnectedComponent



class Normalized(MapTransform):
  """
  Normalizes input
  """

  def __init__(self, keys, meta_key_postfix: str = "meta_dict") -> None:
    super().__init__(keys)
    self.meta_key_postfix = meta_key_postfix
    self.keys = keys

  def __call__(self, volume_node):
    d = dict(volume_node)
    for key in self.keys:
      d[key] = ScaleIntensityRange(a_max=np.amax(d[key]), a_min=np.amin(d[key]), b_max=1.0, b_min=0.0, clip=True)(
        d[key])
    return d


class SlicerLoadImage(MapTransform):
  """
  Adapter from Slicer VolumeNode to MONAI volumes.
  """

  def __init__(self, keys, meta_key_postfix: str = "meta_dict") -> None:
    super().__init__(keys)
    self.meta_key_postfix = meta_key_postfix

  def __call__(self, volume_node):
    data = slicer.util.arrayFromVolume(volume_node)
    data = np.swapaxes(data, 0, 2)
    print("Load volume from Slicer : {}Mb\tshape {}\tdtype {}".format(data.nbytes * 0.000001, data.shape, data.dtype))
    spatial_shape = data.shape
    # apply spacing
    m = vtk.vtkMatrix4x4()
    volume_node.GetIJKToRASMatrix(m)
    affine = slicer.util.arrayFromVTKMatrix(m)
    meta_data = {"affine": affine, "original_affine": affine, "spacial_shape": spatial_shape,
                 'original_spacing': volume_node.GetSpacing()}

    return {self.keys[0]: data, '{}_{}'.format(self.keys[0], self.meta_key_postfix): meta_data}





class PythonDependencyChecker(object):
  """
  Class responsible for installing the Modules dependencies
  """

  @classmethod
  def areDependenciesSatisfied(cls):
    try:
      from packaging import version
      import monai
      import itk
      import torch
      import skimage
      import gdown
      import nibabel

      # Make sure MONAI version is compatible with package
      return version.parse("0.6.0") < version.parse(monai.__version__) <= version.parse("0.9.0")
    except ImportError:
      return False

  @classmethod
  def installDependenciesIfNeeded(cls, progressDialog=None):
    if cls.areDependenciesSatisfied():
      return

    progressDialog = progressDialog or slicer.util.createProgressDialog(maximum=0)
    progressDialog.labelText = "Installing PyTorch"

    try:
      # Try to install the best available pytorch version for the environment using the PyTorch Slicer extension
      import PyTorchUtils
      PyTorchUtils.PyTorchUtilsLogic().installTorch()
    except ImportError:
      # Fallback on default torch available on PIP
      slicer.util.pip_install("torch")

    for dep in ["itk", "nibabel", "scikit-image", "gdown", "monai>0.6.0,<=0.9.0"]:
      progressDialog.labelText = dep
      slicer.util.pip_install(dep)


class MRICTRegistrationCryo(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = "MRICTRegistrationCryo"  
        self.parent.categories = ["Registration"]  
        self.parent.dependencies = []  
        self.parent.contributors = ["Subhra Sundar Goswami, Junichi Takuda"]  
        self.parent.helpText = """
"""
        self.parent.helpText += self.getDefaultModuleDocumentationLink()
        self.parent.acknowledgementText = """
"""
        # Additional initialization step after application startup is complete
        
        moduleDir = os.path.dirname(self.parent.path)
        for iconExtension in ['.svg', '.png']:
          iconPath = os.path.join(moduleDir, 'Resources/Icons', self.__class__.__name__ + iconExtension)
          if os.path.isfile(iconPath):
            parent.icon = qt.QIcon(iconPath)
            break
            

class MRICTRegistrationCryoWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    """
    """
    enableReloadOnSceneClear = True
    
    def __init__(self, parent=None):
        
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)  # needed for parameter node observation
        self.addObserver(slicer.mrmlScene, None, None)
        self.logic = None
        self._parameterNode = None
        self._updatingGUIFromParameterNode = False
        
        self.device = None
        self.modality = None
        self.clippedMasterImageData = None
        self.lastRoiNodeId = ""
        self.lastRoiNodeModifiedTime = 0
        self.roiSelector = slicer.qMRMLNodeComboBox()
        
        
    @staticmethod
    def areDependenciesSatisfied():
        #from RVXLiverSegmentationEffect import PythonDependencyChecker
        # Find extra segment editor effects
        try:
            import SegmentEditorLocalThresholdLib
        except ImportError:
            return False

        return PythonDependencyChecker.areDependenciesSatisfied()

    @staticmethod
    def downloadDependenciesAndRestart():
        #from RVXLiverSegmentationEffect import PythonDependencyChecker
        progressDialog = slicer.util.createProgressDialog(maximum=0)
        extensionManager = slicer.app.extensionsManagerModel()

        def downloadWithMetaData(extName):
            # Method for downloading extensions prior to Slicer 5.0.3
            meta_data = extensionManager.retrieveExtensionMetadataByName(extName)
            if meta_data:
                return extensionManager.downloadAndInstallExtension(meta_data["extension_id"])

        def downloadWithName(extName):
            # Direct extension download since Slicer 5.0.3
            return extensionManager.downloadAndInstallExtensionByName(extName)

        # Install Slicer extensions
        downloadF = downloadWithName if hasattr(extensionManager,
                                            "downloadAndInstallExtensionByName") else downloadWithMetaData

        slicerExtensions = ["SlicerVMTK", "MarkupsToModel", "SegmentEditorExtraEffects", "PyTorch"]
        for slicerExt in slicerExtensions:
            progressDialog.labelText = f"Installing the {slicerExt}\nSlicer extension"
            downloadF(slicerExt)

        # Install PIP dependencies
        PythonDependencyChecker.installDependenciesIfNeeded(progressDialog)
        progressDialog.close()

        # Restart if no extension failed to download. Otherwise warn the user about the failure.
        failedDownload = [slicerExt for slicerExt in slicerExtensions if
                      not extensionManager.isExtensionInstalled(slicerExt)]

        if failedDownload:
            failed_ext_list = "\n".join(failedDownload)
            warning_msg = f"The download process failed install the following extensions : {failed_ext_list}" \
                    f"\n\nPlease try to manually install them using Slicer's extension manager"
            qt.QMessageBox.warning(None, "Failed to download extensions", warning_msg)
        else:
            slicer.app.restart()


    def setup(self):
        
        ScriptedLoadableModuleWidget.setup(self)
        
        self.registrationInProgress = False
        
        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)
        
        
        
        
        # Verify Slicer version compatibility
        if not (slicer.app.majorVersion, slicer.app.minorVersion, float(slicer.app.revision)) >= (4, 11, 29738):
          error_msg = "The RVesselX plugin is only compatible from Slicer 4.11 2021.02.26 onwards.\n" \
                      "Please download the latest Slicer version to use this plugin."
          self.layout.addWidget(qt.QLabel(error_msg))
          self.layout.addStretch()
          slicer.util.errorDisplay(error_msg)
          return

        if not self.areDependenciesSatisfied():
          error_msg = "Slicer VMTK, MarkupsToModel, SegmentEditorExtraEffects and MONAI are required by this plugin.\n" \
                      "Please click on the Download button to download and install these dependencies."
          self.layout.addWidget(qt.QLabel(error_msg))
          downloadDependenciesButton = createButton("Download dependencies and restart",
                                                    self.downloadDependenciesAndRestart)
          self.layout.addWidget(downloadDependenciesButton)
          self.layout.addStretch()
          return
        
        
        
        

        # IO collapsible button
        IOCategory = qt.QWidget()
        self.layout.addWidget(IOCategory)
        IOLayout = qt.QFormLayout(IOCategory)
        self.logic = MRICTRegistrationCryoLogic()
        self.logic.logCallback = self.addLog
        
        inputCollapsibleButton = ctk.ctkCollapsibleButton()
        inputCollapsibleButton.text = "Input Volumes"
        inputCollapsibleButton.collapsed = 0
        self.layout.addWidget(inputCollapsibleButton)
        
        # Layout within the dummy collapsible button
        IOLayout = qt.QFormLayout(inputCollapsibleButton)
        
        self.inputFixedVolumeSelector = slicer.qMRMLNodeComboBox()
        self.inputFixedVolumeSelector.nodeTypes = ["vtkMRMLScalarVolumeNode"]
        self.inputFixedVolumeSelector.selectNodeUponCreation = False
        self.inputFixedVolumeSelector.noneEnabled = False
        self.inputFixedVolumeSelector.addEnabled = False
        self.inputFixedVolumeSelector.removeEnabled = True
        self.inputFixedVolumeSelector.setMRMLScene(slicer.mrmlScene)
        IOLayout.addRow("Input Fixed Volume: ", self.inputFixedVolumeSelector)

        self.inputMovingVolumeSelector = slicer.qMRMLNodeComboBox()
        self.inputMovingVolumeSelector.nodeTypes = ["vtkMRMLScalarVolumeNode"]
        self.inputMovingVolumeSelector.selectNodeUponCreation = False
        self.inputMovingVolumeSelector.noneEnabled = False
        self.inputMovingVolumeSelector.addEnabled = False
        self.inputMovingVolumeSelector.removeEnabled = True
        self.inputMovingVolumeSelector.setMRMLScene(slicer.mrmlScene)
        IOLayout.addRow("Input Moving Volume: ", self.inputMovingVolumeSelector)
        
        #
        # output volume selector
        #
        outputCollapsibleButton = ctk.ctkCollapsibleButton()
        outputCollapsibleButton.text = "Output Volume"
        outputCollapsibleButton.collapsed = 0
        self.layout.addWidget(outputCollapsibleButton)
        
        # Layout within the dummy collapsible button
        IOLayout = qt.QFormLayout(outputCollapsibleButton)
        
        self.outputVolumeSelector = slicer.qMRMLNodeComboBox()
        self.outputVolumeSelector.nodeTypes = ["vtkMRMLScalarVolumeNode"]
        self.outputVolumeSelector.selectNodeUponCreation = False
        self.outputVolumeSelector.addEnabled = True
        self.outputVolumeSelector.removeEnabled = True
        self.outputVolumeSelector.renameEnabled = True
        self.outputVolumeSelector.noneEnabled = False
        self.outputVolumeSelector.showHidden = False
        self.outputVolumeSelector.showChildNodeTypes = False
        self.outputVolumeSelector.setMRMLScene(slicer.mrmlScene)
        self.outputVolumeSelector.setToolTip("Select output volume name.")
        IOLayout.addRow("Output volume:", self.outputVolumeSelector)

        
        #
        # Advanced Area
        #
        advancedCollapsibleButton = ctk.ctkCollapsibleButton()
        advancedCollapsibleButton.text = "Advanced"
        advancedCollapsibleButton.collapsed = 0
        self.layout.addWidget(advancedCollapsibleButton)

        ## Layout within the dummy collapsible button
        advancedFormLayout = qt.QFormLayout(advancedCollapsibleButton)
        
        self.deviceSelector = qt.QComboBox()
        self.deviceSelector.addItems(["cuda", "cpu"])
        advancedFormLayout.addRow("Device:", self.deviceSelector)
        
        ## Add ROI CT options
        self.roiSelectorCT = slicer.qMRMLNodeComboBox()
        self.roiSelectorCT.nodeTypes = ['vtkMRMLMarkupsROINode']
        self.roiSelectorCT.noneEnabled = True
        self.roiSelectorCT.setMRMLScene(slicer.mrmlScene)
        advancedFormLayout.addRow("ROI CT: ", self.roiSelectorCT)
        
        ## Add ROI CT options
        self.roiSelectorMRI = slicer.qMRMLNodeComboBox()
        self.roiSelectorMRI.nodeTypes = ['vtkMRMLMarkupsROINode']
        self.roiSelectorMRI.noneEnabled = True
        self.roiSelectorMRI.setMRMLScene(slicer.mrmlScene)
        advancedFormLayout.addRow("ROI MRI: ", self.roiSelectorMRI)
        
            
        #
        # Apply Button
        #
        self.applyButton = qt.QPushButton("Apply")
        self.applyButton.toolTip = "Start registration."
        self.applyButton.enabled = True #Should be False but changed to True for testing
        self.layout.addWidget(self.applyButton)
        
        
        self.statusLabel = qt.QPlainTextEdit()
        self.statusLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        self.statusLabel.setCenterOnScroll(True)
        self.layout.addWidget(self.statusLabel)
        

        # These connections ensure that whenever user changes some settings on the GUI, that is saved in the MRML scene
        # (in the selected parameter node).
        self.inputFixedVolumeSelector.connect("currentNodeChanged(vtkMRMLNode*)", self.updateParameterNodeFromGUI)
        self.inputMovingVolumeSelector.connect("currentNodeChanged(vtkMRMLNode*)", self.updateParameterNodeFromGUI)
        self.outputVolumeSelector.connect("currentNodeChanged(vtkMRMLNode*)", self.updateParameterNodeFromGUI)
    
        self.applyButton.connect('clicked(bool)', self.onApplyButton)
        
        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()
        
    def cleanup(self):
        """
        Called when the application closes and the module widget is destroyed.
        """
        self.removeObservers()
        
        
    def enter(self):
        """
        Called each time the user opens this module.
        """
        # Make sure parameter node exists and observed
        self.initializeParameterNode()

    def exit(self):
        """
        Called each time the user opens a different module.
        """
        # Do not react to parameter node changes (GUI wlil be updated when the user enters into the module)
        self.removeObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self.updateGUIFromParameterNode)

    def onSceneStartClose(self, caller, event):
        """
        Called just before the scene is closed.
        """
        # Parameter node will be reset, do not use it anymore
        self.setParameterNode(None)

    def onSceneEndClose(self, caller, event):
        """
        Called just after the scene is closed.
        """
        # If this module is shown while the scene is closed then recreate a new parameter node immediately
        if self.parent.isEntered:
            self.initializeParameterNode()

    def initializeParameterNode(self):
        """
        Ensure parameter node exists and observed.
        """
        self.setParameterNode(self.logic.getParameterNode())

        
    def setParameterNode(self, inputParameterNode):
        """
        Set and observe parameter node.
        Observation is needed because when the parameter node is changed then the GUI must be updated immediately.
        We will implement it later
        """

        if inputParameterNode:
            self.logic.setDefaultParameters(inputParameterNode)

        # Unobserve previously selected parameter node and add an observer to the newly selected.
        # Changes of parameter node are observed so that whenever parameters are changed by a script or any other module those are reflected immediately in the GUI.
        if self._parameterNode is not None and self.hasObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self.updateGUIFromParameterNode):self.removeObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self.updateGUIFromParameterNode)
        self._parameterNode = inputParameterNode
        if self._parameterNode is not None:self.addObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self.updateGUIFromParameterNode)

        # Initial GUI update
        self.updateGUIFromParameterNode()

        
    def updateGUIFromParameterNode(self, caller=None, event=None):
        """
        This method is called whenever parameter node is changed.
        The module GUI is updated to show the current state of the parameter node.
        """

        if self._parameterNode is None or self._updatingGUIFromParameterNode:
            return

        # Make sure GUI changes do not call updateParameterNodeFromGUI (it could cause infinite loop)
        self._updatingGUIFromParameterNode = True

        # Update node selectors and sliders
        self.inputFixedVolumeSelector.setCurrentNode(self._parameterNode.GetNodeReference("InputFixedVolume"))
        self.inputMovingVolumeSelector.setCurrentNode(self._parameterNode.GetNodeReference("InputMovingVolume"))
        self.outputVolumeSelector.setCurrentNode(self._parameterNode.GetNodeReference("OutputVolume"))

        # Update buttons states and tooltips
        if self._parameterNode.GetNodeReference("InputFixedVolume") and self._parameterNode.GetNodeReference("InputMovingVolume"):
         #and self._parameterNode.GetNodeReference("OutputVolume")"""
            self.applyButton.toolTip = "Compute output Volume"
            self.applyButton.enabled = True
        else:
            self.applyButton.toolTip = "Select input and output volume nodes"
            self.applyButton.enabled = True #Should be False but changed to True for testing

        # All the GUI updates are done
        self._updatingGUIFromParameterNode = True
        
        # Add vertical spacer
        # self.layout.addStretch(2)
        
        
    def updateParameterNodeFromGUI(self, caller=None, event=None):
        """
        This method is called when the user makes any change in the GUI.
        The changes are saved into the parameter node (so that they are restored when the scene is saved and loaded).
        """

        if self._parameterNode is None or self._updatingGUIFromParameterNode:
            return

        wasModified = self._parameterNode.StartModify()  # Modify all properties in a single batch

        self._parameterNode.SetNodeReferenceID("InputFixedVolume", self.inputFixedVolumeSelector.currentNodeID)
        self._parameterNode.SetNodeReferenceID("InputMovingVolume", self.inputMovingVolumeSelector.currentNodeID)
        self._parameterNode.SetNodeReferenceID("OutputVolume", self.outputVolumeSelector.currentNodeID)
        self._parameterNode.EndModify(wasModified)

    
    def onSelect(self):

        #self.applyButton.enabled = self.inputFixedVolumeSelector.currentNode() and self.inputMovingVolumeSelector.currentNode() """and self.outputVolumeSelector.currentNode()"""

        if not self.registrationInProgress:
          self.applyButton.text = "Apply"
          return
        self.updateBrowsers()
            
        
    def onApplyButton(self):
        """
        Run processing when user clicks "Apply" button.
        """
        
        if self.registrationInProgress:
          self.registrationInProgress = False
          self.abortRequested = True
          raise ValueError("User requested cancel.")
          self.cliNode.Cancel() # not implemented
          self.applyButton.text = "Cancelling..."
          self.applyButton.enabled = True   #Should be False but changed to True for testing
          return

        self.registrationInProgress = True
        self.applyButton.text = "Cancel"
        self.statusLabel.plainText = ''
        slicer.app.setOverrideCursor(qt.Qt.WaitCursor)
        
        try:

            self.logic.process(self.inputFixedVolumeSelector.currentNode(),
                        self.inputMovingVolumeSelector.currentNode(), self.outputVolumeSelector.currentNode())

            time.sleep(3)
            
        except Exception as e:
          print(e)
          self.addLog("Error: {0}".format(str(e)))
          import traceback
          traceback.print_exc()
          
        finally:
          slicer.app.restoreOverrideCursor()
          self.registrationInProgress = False
          self.onSelect() # restores default Apply button state
            
            

    def addLog(self, text):
        """Append text to log window
        """
        self.statusLabel.appendPlainText(text)
        slicer.app.processEvents()  # force update



class MRICTRegistrationCryoLogic(ScriptedLoadableModuleLogic):
    """This class should implement all the actual computation done by the module.
    """

    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)

    def setDefaultParameters(self, parameterNode):
        A = 100

    def process(self, inputFixedVolume, inputMovingVolume, outputVolume):
        """
        Run the processing algorithm.
        """
        
        if not inputFixedVolume or not inputMovingVolume or not outputVolume:
            raise ValueError("Input or output volume is missing or invalid")

        startTime = time.time()
        logging.info('Processing started')
        
        # Start execution in the background
        
        #Segment the liver from CT using AI based segmentation module RVX
        inputFixedVolumeMask = slicer.vtkMRMLScalarVolumeNode()
        inputFixedVolumeMask.SetName('inputFixedVolumeMask')
        #slicer.mrmlScene.AddNode(inputFixedVolumeMask)
        self.f_segmentationMask(inputFixedVolume, inputFixedVolumeMask, "cpu", "CT")
        
        #Correct bias using N4 filter
        movingVolumeN4 = self.f_n4itkbiasfieldcorrection(inputMovingVolume)
        movingVolumeN4.SetName('movingVolumeN4')
        slicer.mrmlScene.AddNode(movingVolumeN4)
        
        #Segment the liver from MRI using AI based segmentation module RVX
        inputMovingVolumeMask = slicer.vtkMRMLScalarVolumeNode()
        inputMovingVolumeMask.SetName('inputMovingVolumeMask')
        slicer.mrmlScene.AddNode(inputMovingVolumeMask)
        self.f_segmentationMask(movingVolumeN4, inputMovingVolumeMask, "cpu", "MRI")
        
        
        self.f_registrationBrainsFit(inputFixedVolume, inputMovingVolume, outputVolume)
        
        stopTime = time.time()
        logging.info(f'Processing completed in {stopTime-startTime:.2f} seconds')
        
    def f_n4itkbiasfieldcorrection(self, inputVolumeNode):
        
        # Set parameters
        parameters = {}
        parameters["inputImageName"] = inputVolumeNode
                
        outputVolumeNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode")
        parameters["outputImageName"] = outputVolumeNode
        
        N4BiasFilter = slicer.modules.n4itkbiasfieldcorrection
        cliNode = slicer.cli.runSync(N4BiasFilter, None, parameters)
        if cliNode.GetStatus() & cliNode.ErrorsMask:
            # error
            errorText = cliNode.GetErrorText()
            slicer.mrmlScene.RemoveNode(cliNode)
            raise ValueError("CLI execution failed: " + errorText)
          # success
        return outputVolumeNode
          
    def f_segmentationMask(self, inputVolumeNode, outputVolumeNode, use_cudaOrCpu, modalityV):
       
        #Have to implement it to have the processing only on the ROI selected
        slicer.vtkSlicerSegmentationsModuleLogic.CopyOrientedImageDataToVolumeNode(self.getClippedMasterImageData(), inputVolumeNode)
        
       
        try:
            self.launchLiverSegmentation(inputVolumeNode, outputVolumeNode, use_cudaOrCpu, modalityV)

        except Exception as e:
            qt.QApplication.restoreOverrideCursor()
            slicer.util.errorDisplay(str(e))

        finally:
            qt.QApplication.restoreOverrideCursor()
    
    
    #
    def getClippedMasterImageData(self):
        """
        Crops the master volume node if a ROI Node is selected in the parameter comboBox. Otherwise returns the full extent
        of the volume.
        """
        # Return masterImageData unchanged if there is no ROI
        masterImageData = MRICTRegistrationCryoWidget.advancedFormLayout.masterVolumeImageData()
        roiNode = self.roiSelector.currentNode()
        if roiNode is None or masterImageData is None:
          self.clippedMasterImageData = None
          self.lastRoiNodeId = ""
          self.lastRoiNodeModifiedTime = 0
          return masterImageData

        # Return last clipped image data if there was no change
        if (
            self.clippedMasterImageData is not None and roiNode.GetID() == self.lastRoiNodeId and roiNode.GetMTime() == self.lastRoiNodeModifiedTime):
          # Use cached clipped master image data
          return self.clippedMasterImageData

        # Compute clipped master image
        import SegmentEditorLocalThresholdLib
        self.clippedMasterImageData = SegmentEditorLocalThresholdLib.SegmentEditorEffect.cropOrientedImage(masterImageData,
                                                                                                           roiNode)
        self.lastRoiNodeId = roiNode.GetID()
        self.lastRoiNodeModifiedTime = roiNode.GetMTime()
        return self.clippedMasterImageData
        
        
          
    def f_registrationBrainsFit(self, inputFixedVolume, inputMovingVolume, outputVolume):
        
        # Set parameters
        
        fixedVolumeID = inputFixedVolume.GetID()
        movingVolumeID = inputMovingVolume.GetID()
        outputVolumeID = outputVolume.GetID()
        
        parameters = {}
        parameters["fixedVolume"] = fixedVolumeID
        parameters["movingVolume"] = movingVolumeID
        parameters["outputVolume"] = outputVolumeID
        parameters["initializeTransformMode"] = "useMomentsAlign"
        parameters["useRigid"] = True
        parameters["useScaleVersor3D"] = True
        parameters["useScaleSkewVersor3D"] = True
        parameters["useAffine"] = True
        #parameters["linearTransform"] = self.__movingTransform.GetID()

        self.__cliNode = None
        slicer.cli.run(slicer.modules.brainsfit, self.__cliNode, parameters)

        #self.__cliObserverTag = self.__cliNode.AddObserver('ModifiedEvent', self.processRegistrationCompletion)
        #self.__registrationStatus.setText('Wait ...')
        #self.__registrationButton.setEnabled(0)
        
        
    @classmethod
    def createUNetModel(cls, device):
        return UNet(dimensions=3, in_channels=1, out_channels=2, channels=(16, 32, 64, 128, 256), strides=(2, 2, 2, 2),
                    num_res_units=2, norm=Norm.BATCH, ).to(device)

    @classmethod
    def getPreprocessingTransform(cls, modality):
        """
        Preprocessing transform which converts the input volume to MONAI format and resamples and normalizes its inputs.
        The values in this transform are the same as in the training transform preprocessing.
        """
        if modality == "CT":
            trans = [SlicerLoadImage(keys=["image"]), AddChanneld(keys=["image"]), Spacingd(keys=["image"], pixdim=(1.5, 1.5, 2.0), mode="bilinear"), Orientationd(keys=["image"], axcodes="RAS"), ScaleIntensityRanged(keys=["image"], a_min=-57, a_max=164, b_min=0.0, b_max=1.0, clip=True), AddChanneld(keys=["image"]), ToTensord(keys=["image"])]
                   
            return Compose(trans)
            
        elif modality == "MRI":
            trans = [SlicerLoadImage(keys=["image"]), AddChanneld(keys=["image"]), Spacingd(keys=["image"], pixdim=(1.5, 1.5, 2.0), mode="bilinear"), Orientationd(keys=["image"], axcodes="LPS"), Normalized(keys=["image"]), AddChanneld(keys=["image"]), ToTensord(keys=["image"])]
                   
            return Compose(trans)

    @classmethod
    def getPostProcessingTransform(cls, original_spacing, original_size, modality):
        """
        Simple post processing transform to convert the volume back to its original spacing.
        """
        return Compose([
            AddChanneld(keys=["image"]),
            Spacingd(keys=["image"], pixdim=original_spacing, mode="nearest"),
            Resized(keys=["image"], spatial_size=original_size)
        ])

    @classmethod
    def launchLiverSegmentation(cls, in_volume_node, out_volume_node, use_cuda, modality):
        """
        Runs the segmentation on the input volume and returns the segmentation in the same volume.
        """
        device = torch.device("cpu") if not use_cuda or not torch.cuda.is_available() else torch.device("cuda:0")
        print("Start liver segmentation using device :", device)
        print(f"Using modality {modality}")
        try:
          with torch.no_grad():
            model_path = os.path.join(os.path.dirname(__file__),
                                      "liver_ct_model.pt" if modality == "CT" else "liver_mri_model.pt")
            print("Model path: ", model_path)
            model = cls.createUNetModel(device=device)
            model.load_state_dict(torch.load(model_path, map_location=device))
            print("Model loaded .. ")
            transform_output = cls.getPreprocessingTransform(modality)(in_volume_node)
            print("Transform with MONAI applied .. ")
            model_input = transform_output["image"].to(device)

            print("Run UNet model on input volume")

            roi_size = (160, 160, 160) if modality == "CT" else (240, 240, 96)

            model_output = sliding_window_inference(model_input, roi_size, 4, model, device="cpu", sw_device=device)

            print("Keep largest connected components and threshold UNet output")
            discrete_output = AsDiscrete(argmax=True)(model_output.reshape(model_output.shape[-4:]))
            post_processed = KeepLargestConnectedComponent(applied_labels=[1])(discrete_output)
            output_volume = post_processed.cpu().numpy()[0, :, :, :]
            
            print(transform_output["image"])
            print(transform_output["image"].max())
            
            del post_processed, discrete_output, model_output, model, model_input

            transform_output["image"] = output_volume
            original_spacing = (transform_output["image_meta_dict"]["original_spacing"])
            original_size = (transform_output["image_meta_dict"]["spacial_shape"])
            output_inverse_transform = cls.getPostProcessingTransform(original_spacing, original_size, modality)(
              transform_output)

            label_map_input = output_inverse_transform["image"][0, :, :, :]

            print("output label map shape is " + str(label_map_input.shape))

            output_affine_matrix = transform_output["image_meta_dict"]["affine"]

            out_volume_node.SetIJKToRASMatrix(slicer.util.vtkMatrixFromArray(output_affine_matrix))
            slicer.util.updateVolumeFromArray(out_volume_node, np.swapaxes(label_map_input, 0, 2))
            del transform_output

        finally:
            # Cleanup any remaining memory
            def del_local(v):
                if v in locals():
                    del locals()[v]

            for n in ["model_input", "model_output", "post_processed", "model", "transform_output"]:
                del_local(n)

            gc.collect()
            torch.cuda.empty_cache()
          
      
      
      
      
      

class MRICTRegistrationCryoTest(ScriptedLoadableModuleTest):
    """
    This is the test case for the scripted module MRICTRegistrationCryo.
    """

    def setUp(self):
        slicer.mrmlScene.Clear()

    def runTest(self):
        self.setUp()
        self.test_MRICTRegistration()

    def test_MRICTRegistration(self):
        """
        Should test the algorithm with test dataset
        """

        self.delayDisplay("Starting the test")

        # Get/create input data

        # import SampleData
        # registerSampleData()
        # inputVolume = SampleData.downloadSample('CryoAblation1')
        # self.delayDisplay('Loaded test data set')

        # outputVolume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode")

        # Test the module logic
        # logic = MRICTRegistrationCryoLogic()

        # Test algorithm
        # logic.process(self, inputFixedVolume, inputMovingVolume, outputVolume)

        self.delayDisplay('Test passed')

