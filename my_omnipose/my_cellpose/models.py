import os, sys, time, shutil, tempfile, datetime, pathlib, subprocess
from pprint import PrettyPrinter
from pathlib import Path
import numpy as np
from tqdm import trange, tqdm
from urllib.parse import urlparse
import torch
from torch import nn, distributed
# from torch.nn.parallel import DistributedDataParallel as DDP

from scipy.ndimage import gaussian_filter, zoom

import logging
models_logger = logging.getLogger(__name__)

from . import transforms, dynamics, utils, plot
from .core import UnetModel, assign_device, check_mkl, MXNET_ENABLED, parse_model_string
from .io import OMNI_INSTALLED

_MODEL_URL = 'https://www.cellpose.org/models'
_MODEL_DIR_ENV = os.environ.get("CELLPOSE_LOCAL_MODELS_PATH")
_MODEL_DIR_DEFAULT = pathlib.Path.home().joinpath('.cellpose', 'models')
MODEL_DIR = pathlib.Path(_MODEL_DIR_ENV) if _MODEL_DIR_ENV else _MODEL_DIR_DEFAULT

if OMNI_INSTALLED:
    import my_omnipose
    from my_omnipose.core import OMNI_MODELS
else:
    OMNI_MODELS = []
    
MODEL_NAMES = ['cyto','nuclei','cyto2']+OMNI_MODELS

def model_path(model_type, model_index, use_torch):
    torch_str = 'torch' if use_torch else ''
    basename = '%s%s_%d' % (model_type, torch_str, model_index)
    return cache_model_path(basename)

def size_model_path(model_type, use_torch):
    torch_str = 'torch' if use_torch else ''
    basename = 'size_%s%s_0.npy' % (model_type, torch_str)
    return cache_model_path(basename)

def cache_model_path(basename):
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    url = f'{_MODEL_URL}/{basename}'
    cached_file = os.fspath(MODEL_DIR.joinpath(basename)) 
    if not os.path.exists(cached_file):
        models_logger.info('Downloading: "{}" to {}\n'.format(url, cached_file))
        print(url,cached_file)
        utils.download_url_to_file(url, cached_file, progress=True)
    return cached_file

def deprecation_warning_cellprob_dist_threshold(cellprob_threshold, dist_threshold):
    models_logger.warning('cellprob_threshold and dist_threshold are being deprecated in a future release, use mask_threshold instead')
    return cellprob_threshold if cellprob_threshold is not None else dist_threshold


class Cellpose():
    """ main model which combines SizeModel and CellposeModel

    Parameters
    ----------

    gpu: bool (optional, default False)
        whether or not to use GPU, will check if GPU available

    model_type: str (optional, default 'cyto')
        'cyto'=cytoplasm model; 'nuclei'=nucleus model

    net_avg: bool (optional, default True)
        loads the 4 built-in networks and averages them if True, loads one network if False

    device: gpu device (optional, default None)
        where model is saved (e.g. mx.gpu() or mx.cpu()), overrides gpu input,
        recommended if you want to use a specific GPU (e.g. mx.gpu(4) or torch.cuda.device(4))

    torch: bool (optional, default True)
        run model using torch if available

    """
    def __init__(self, gpu=False, model_type='cyto', pretrained_model=None, pretrained_size=None, net_avg=True, device=None, 
                 torch=True, model_dir=None, dim=2, omni=None):
        super(Cellpose, self).__init__()
        if not torch:
            if not MXNET_ENABLED:
                torch = True
        self.torch = torch
        
        # assign device (GPU or CPU)
        sdevice, gpu = assign_device(self.torch, gpu)
        self.device = device if device is not None else sdevice
        self.gpu = gpu

        
        # set defaults and catch if cyto2 is being used without torch 
        model_type = 'cyto' if model_type is None else model_type
        if model_type=='cyto2' and not self.torch:
            model_type='cyto'
        
        # default omni on if model specifies this, but also override by user setting 
        self.omni = ('omni' in model_type) if omni is None else omni 
        self.dim = dim # 2D vs 3D
        
        # for now, omni models cannot do net_avg 
        if self.omni:
            net_avg = False
        model_range = range(4) if net_avg else range(1)
        
        if pretrained_model == None:
            self.pretrained_model = [model_path(model_type, j, torch) for j in model_range]
        else:
            self.pretrained_model = [pretrained_model]

        self.diam_mean = 30. #default for any cyto model 
        nuclear = 'nuclei' in model_type
        bacterial = ('bact' in model_type) or ('worm' in model_type) 
        
        if nuclear:
            self.diam_mean = 17. 
        elif bacterial:
            #self.diam_mean = 0.
            net_avg = False # No bacterial or omni models have additional models
        
        if not net_avg:
            self.pretrained_model = self.pretrained_model[0]
    
        self.cp = CellposeModel(device=self.device, gpu=self.gpu,
                                pretrained_model=self.pretrained_model,
                                diam_mean=self.diam_mean, torch=self.torch, 
                                dim=self.dim, omni=self.omni)
        self.cp.model_type = model_type
        
        # size model not used for bacterial model
        if not bacterial:
            if pretrained_size == None:
                self.pretrained_size = size_model_path(model_type, torch)
            else:
                self.pretrained_size = pretrained_size
            self.sz = SizeModel(device=self.device, pretrained_size=self.pretrained_size,
                                cp_model=self.cp)
            self.sz.model_type = model_type
        else:
            self.pretrained_size = None

    def eval(self, x, batch_size=8, channels=None, channel_axis=None, z_axis=None,
             invert=False, normalize=True, diameter=30., do_3D=False, anisotropy=None,
             net_avg=True, augment=False, tile=True, tile_overlap=0.1, resample=True, 
             interp=True, cluster=False, flow_threshold=0.4, mask_threshold=0.0, 
             cellprob_threshold=None, dist_threshold=None, diam_threshold=12., min_size=15,
             stitch_threshold=0.0, rescale=None, progress=None, omni=False, verbose=False,
             transparency=False, model_loaded=False):
        """ run cellpose and get masks

        Parameters
        ----------
        x: list or array of images
            can be list of 2D/3D images, or array of 2D/3D images, or 4D image array

        batch_size: int (optional, default 8)
            number of 224x224 patches to run simultaneously on the GPU
            (can make smaller or bigger depending on GPU memory usage)

        channels: list (optional, default None)
            list of channels, either of length 2 or of length number of images by 2.
            First element of list is the channel to segment (0=grayscale, 1=red, 2=green, 3=blue).
            Second element of list is the optional nuclear channel (0=none, 1=red, 2=green, 3=blue).
            For instance, to segment grayscale images, input [0,0]. To segment images with cells
            in green and nuclei in blue, input [2,3]. To segment one grayscale image and one
            image with cells in green and nuclei in blue, input [[0,0], [2,3]].
        
        channel_axis: int (optional, default None)
            if None, channels dimension is attempted to be automatically determined

        z_axis: int (optional, default None)
            if None, z dimension is attempted to be automatically determined

        invert: bool (optional, default False)
            invert image pixel intensity before running network (if True, image is also normalized)

        normalize: bool (optional, default True)
                normalize data so 0.0=1st percentile and 1.0=99th percentile of image intensities in each channel

        diameter: float (optional, default 30.)
            if set to None, then diameter is automatically estimated if size model is loaded

        do_3D: bool (optional, default False)
            set to True to run 3D segmentation on 4D image input

        anisotropy: float (optional, default None)
            for 3D segmentation, optional rescaling factor (e.g. set to 2.0 if Z is sampled half as dense as X or Y)

        net_avg: bool (optional, default True)
            runs the 4 built-in networks and averages them if True, runs one network if False

        augment: bool (optional, default False)
            tiles image with overlapping tiles and flips overlapped regions to augment

        tile: bool (optional, default True)
            tiles image to ensure GPU/CPU memory usage limited (recommended)

        tile_overlap: float (optional, default 0.1)
            fraction of overlap of tiles when computing flows

        resample: bool (optional, default True)
            run dynamics at original image size (will be slower but create more accurate boundaries)

        interp: bool (optional, default True)
                interpolate during 2D dynamics (not available in 3D) 
                (in previous versions it was False)

        flow_threshold: float (optional, default 0.4)
            flow error threshold (all cells with errors below threshold are kept) (not used for 3D)

        mask_threshold: float (optional, default 0.0)
            all pixels with value above threshold kept for masks, decrease to find more and larger masks
        
        dist_threshold: float (optional, default None) DEPRECATED
            use mask_threshold instead

        cellprob_threshold: float (optional, default None) DEPRECATED
            use mask_threshold instead

        min_size: int (optional, default 15)
                minimum number of pixels per mask, can turn off with -1

        stitch_threshold: float (optional, default 0.0)
            if stitch_threshold>0.0 and not do_3D and equal image sizes, masks are stitched in 3D to return volume segmentation

        rescale: float (optional, default None)
            if diameter is set to None, and rescale is not None, then rescale is used instead of diameter for resizing image

        progress: pyqt progress bar (optional, default None)
            to return progress bar status to GUI
            
        omni: bool (optional, default False)
            use omnipose mask recontruction features

        calc_trace: bool (optional, default False)
            calculate pixel traces and return as part of the flow

        verbose: bool (optional, default False)
            turn on additional output to logs for debugging 

        verbose: bool (optional, default False)
            turn on additional output to logs for debugging

        transparency: bool (optional, default False)
            modulate flow opacity by magnitude instead of brightness (can use flows on any color background) 

        model_loaded: bool (optional, default False)
            internal variable for determining if model has been loaded, used in __main__.py

        Returns
        -------
        masks: list of 2D arrays, or single 3D array (if do_3D=True)
                labelled image, where 0=no masks; 1,2,...=mask labels

        flows: list of lists 2D arrays, or list of 3D arrays (if do_3D=True)
            flows[k][0] = XY flow in HSV 0-255
            flows[k][1] = flows at each pixel
            flows[k][2] = scalar cell probability (Cellpose) or distance transform (Omnipose)
            flows[k][3] = final pixel locations after Euler integration 
            flows[k][4] = boundary output (nonempty for Omnipose)
            flows[k][5] = pixel traces (nonempty for calc_trace=True)

        styles: list of 1D arrays of length 256, or single 1D array (if do_3D=True)
            style vector summarizing each image, also used to estimate size of objects in image

        diams: list of diameters, or float (if do_3D=True)

        """        

        if cellprob_threshold is not None or dist_threshold is not None:
            mask_threshold = deprecation_warning_cellprob_dist_threshold(cellprob_threshold, dist_threshold)

        tic0 = time.time()
        channels = [0,0] if channels is None else channels # why not just make this a default in the function header?

        estimate_size = True if (diameter is None or diameter==0) else False
        if estimate_size and self.pretrained_size is not None and not do_3D and x[0].ndim < 4:
            tic = time.time()
            models_logger.info('~~~ ESTIMATING CELL DIAMETER(S) ~~~')
            diams, _ = self.sz.eval(x, channels=channels, channel_axis=channel_axis, 
                                    invert=invert, batch_size=batch_size, 
                                    augment=augment, tile=tile, normalize=normalize)
            rescale = self.diam_mean / np.array(diams)
            diameter = None
            models_logger.info('estimated cell diameter(s) in %0.2f sec'%(time.time()-tic))
            models_logger.info('>>> diameter(s) = ')
            if isinstance(diams, list) or isinstance(diams, np.ndarray):
                diam_string = '[' + ''.join(['%0.2f, '%d for d in diams]) + ']'
            else:
                diam_string = '[ %0.2f ]'%diams
            models_logger.info(diam_string)
        elif estimate_size:
            if self.pretrained_size is None:
                reason = 'no pretrained size model specified in model Cellpose'
            else:
                reason = 'does not work on non-2D images'
            models_logger.warning(f'could not estimate diameter, {reason}')
            diams = self.diam_mean 
        else:
            diams = diameter

        tic = time.time()
        models_logger.info('~~~ FINDING MASKS ~~~')
        masks, flows, styles = self.cp.eval(x, 
                                            batch_size=batch_size, 
                                            invert=invert, 
                                            normalize=normalize,
                                            diameter=diameter,
                                            rescale=rescale, 
                                            anisotropy=anisotropy, 
                                            channels=channels,
                                            channel_axis=channel_axis, 
                                            z_axis=z_axis,
                                            augment=augment, 
                                            tile=tile, 
                                            do_3D=do_3D, 
                                            net_avg=net_avg, 
                                            progress=progress,
                                            tile_overlap=tile_overlap,
                                            resample=resample,
                                            interp=interp,
                                            cluster=cluster,
                                            flow_threshold=flow_threshold, 
                                            mask_threshold=mask_threshold,
                                            diam_threshold=diam_threshold,
                                            min_size=min_size, 
                                            stitch_threshold=stitch_threshold,
                                            omni=omni,
                                            verbose=verbose,
                                            transparency=transparency,
                                            model_loaded=model_loaded)
        models_logger.info('>>>> TOTAL TIME %0.2f sec'%(time.time()-tic0))
    
        return masks, flows, styles, diams

    
# there is a bunch of repetiton in cellpose(), cellposemodel(), __main__ with nuclear, bacterial checks
# I need to figure out a way to fcotr all that out, probably by making a function in models and calling it
# in all three contexts. Also I should just check for model existence for the 4-model averaging instead of 
# requiring it for models based on name. 
class CellposeModel(UnetModel):
    """

    Parameters
    -------------------

    gpu: bool (optional, default False)
        whether or not to save model to GPU, will check if GPU available
        
    pretrained_model: str or list of strings (optional, default False)
        path to pretrained cellpose model(s), if None or False, no model loaded
        
    model_type: str (optional, default None)
        'cyto'=cytoplasm model; 'nuclei'=nucleus model; if None, pretrained_model used
        
    net_avg: bool (optional, default True)
        loads the 4 built-in networks and averages them if True, loads one network if False
        
    torch: bool (optional, default True)
        use torch nn rather than mxnet
        
    diam_mean: float (optional, default 27.)
        mean 'diameter', 27. is built in value for 'cyto' model
        
    device: mxnet device (optional, default None)
        where model is saved (mx.gpu() or mx.cpu()), overrides gpu input,
        recommended if you want to use a specific GPU (e.g. mx.gpu(4))
        
    model_dir: str (optional, default None)
        overwrite the built in model directory where cellpose looks for models
    
    omni: use omnipose model (optional, default False)

    """
    
    def __init__(self, gpu=False, pretrained_model=False,
                 model_type=None, net_avg=True, torch=True,
                 diam_mean=30., device=None,
                 residual_on=True, style_on=True, concatenation=False,
                 nchan=2, nclasses=3, dim=2, omni=False, 
                 checkpoint=False, dropout=False, kernel_size=2):
        if not torch:
            if not MXNET_ENABLED:
                torch = True
        self.torch = torch
        if isinstance(pretrained_model, np.ndarray):
            pretrained_model = list(pretrained_model)
        elif isinstance(pretrained_model, str):
            pretrained_model = [pretrained_model]
    
        # initialize according to arguments 
        # these are overwritten if a model requires it (bact_omni the most rectrictive)
        self.omni = omni
        self.nclasses = nclasses 
        self.diam_mean = diam_mean
        self.dim = dim # 2D vs 3D
        self.nchan = nchan 
        self.checkpoint = checkpoint
        self.dropout = dropout
        self.kernel_size = kernel_size
        # channel axis might be useful here 
        if model_type is not None or (pretrained_model and not os.path.exists(pretrained_model[0])):
            pretrained_model_string = model_type 
            if ~np.any([pretrained_model_string == s for s in MODEL_NAMES]): #also covers None case
                pretrained_model_string = 'cyto'
            if (pretrained_model and not os.path.exists(pretrained_model[0])):
                models_logger.warning('pretrained model has incorrect path')
            models_logger.info(f'>>{pretrained_model_string}<< model set to be used')
            
            nuclear = 'nuclei' in pretrained_model_string
            bacterial = ('bact' in pretrained_model_string) or ('worm' in pretrained_model_string) 
            
            if nuclear:
                self.diam_mean = 17. 
            elif bacterial:
                #self.diam_mean = 0.
                net_avg = False #'bact' models has no 1,2,3 yet, nor do any omni models 

            # set omni flag to true if the name contains it
            self.omni = 'omni' in os.path.splitext(Path(pretrained_model_string).name)[0]
            
            # for now, omni models cannot do net_avg 
            if self.omni:
                net_avg = False
                
            #changed to only look for multiple files if net_avg is selected
            model_range = range(4) if net_avg else range(1)
            pretrained_model = [model_path(pretrained_model_string, j, torch) for j in model_range]
            residual_on, style_on, concatenation = True, True, False
        else:
            if pretrained_model:
                pretrained_model_string = pretrained_model[0]
                params = parse_model_string(pretrained_model_string)
                if params is not None:
                    residual_on, style_on, concatenation = params 
                self.omni = 'omni' in os.path.splitext(Path(pretrained_model_string).name)[0]
        
        # Omni models have 4D output for 2D images, 5D for 3D images, etc. (flow compinents increase with dimension)
        # Note that omni can still be used independently for evaluation to 'mix and match'
        #would be better just to read from the model 
        if self.omni:
            self.nclasses = self.dim + 2          

        # initialize network
        super().__init__(gpu=gpu, pretrained_model=False,
                         diam_mean=self.diam_mean, net_avg=net_avg, device=device,
                         residual_on=residual_on, style_on=style_on, concatenation=concatenation,
                         nclasses=self.nclasses, torch=self.torch, nchan=self.nchan, 
                         dim=self.dim, checkpoint=self.checkpoint, dropout=self.dropout,
                         kernel_size=self.kernel_size)


        self.unet = False
        self.pretrained_model = pretrained_model
        if self.pretrained_model and len(self.pretrained_model)==1:
            
            # # dataparallel
            # if self.torch and self.gpu:
            #     net = self.net.module
            # else:
            #     net = self.net
            
            self.net.load_model(self.pretrained_model[0], cpu=(not self.gpu))
            if not self.torch:
                self.net.collect_params().grad_req = 'null'
        ostr = ['off', 'on']
        omnistr = ['','_omni'] #toggle by containing omni phrase 
        self.net_type = 'cellpose_residual_{}_style_{}_concatenation_{}{}'.format(ostr[residual_on],
                                                                                   ostr[style_on],
                                                                                   ostr[concatenation],
                                                                                   omnistr[omni]) 
        
        if self.torch and gpu:
            self.net = nn.DataParallel(self.net)
            
            # distributed.init_process_group()
            # distributed.launch
            # distributed.init_process_group(backend='nccl')
            # self.net = nn.parallel.DistributedDataParallel(self.net)
            
            
    
    def eval(self, x, batch_size=8, channels=None, channel_axis=None, 
             z_axis=None, normalize=True, invert=False, 
             rescale=None, diameter=None, do_3D=False, anisotropy=None, net_avg=True, 
             augment=False, tile=True, tile_overlap=0.1,
             resample=True, interp=True, cluster=False,
             flow_threshold=0.4, mask_threshold=0.0, diam_threshold=12.,
             cellprob_threshold=None, dist_threshold=None, flow_factor=5.0,
             compute_masks=True, min_size=15, stitch_threshold=0.0, progress=None, omni=False, 
             calc_trace=False, verbose=False, transparency=False, loop_run=False, model_loaded=False):
        """
            segment list of images x, or 4D array - Z x nchan x Y x X

            Parameters
            ----------
            x: list or array of images
                can be list of 2D/3D/4D images, or array of 2D/3D/4D images

            batch_size: int (optional, default 8)
                number of 224x224 patches to run simultaneously on the GPU
                (can make smaller or bigger depending on GPU memory usage)

            channels: list (optional, default None)
                list of channels, either of length 2 or of length number of images by 2.
                First element of list is the channel to segment (0=grayscale, 1=red, 2=green, 3=blue).
                Second element of list is the optional nuclear channel (0=none, 1=red, 2=green, 3=blue).
                For instance, to segment grayscale images, input [0,0]. To segment images with cells
                in green and nuclei in blue, input [2,3]. To segment one grayscale image and one
                image with cells in green and nuclei in blue, input [[0,0], [2,3]].

            channel_axis: int (optional, default None)
                if None, channels dimension is attempted to be automatically determined

            z_axis: int (optional, default None)
                if None, z dimension is attempted to be automatically determined

            normalize: bool (default, True)
                normalize data so 0.0=1st percentile and 1.0=99th percentile of image intensities in each channel

            invert: bool (optional, default False)
                invert image pixel intensity before running network

            rescale: float (optional, default None)
                resize factor for each image, if None, set to 1.0

            diameter: float (optional, default None)
                diameter for each image (only used if rescale is None), 
                if diameter is None, set to diam_mean

            do_3D: bool (optional, default False)
                set to True to run 3D segmentation on 4D image input

            anisotropy: float (optional, default None)
                for 3D segmentation, optional rescaling factor (e.g. set to 2.0 if Z is sampled half as dense as X or Y)

            net_avg: bool (optional, default True)
                runs the 4 built-in networks and averages them if True, runs one network if False

            augment: bool (optional, default False)
                tiles image with overlapping tiles and flips overlapped regions to augment

            tile: bool (optional, default True)
                tiles image to ensure GPU/CPU memory usage limited (recommended)

            tile_overlap: float (optional, default 0.1)
                fraction of overlap of tiles when computing flows

            resample: bool (optional, default True)
                run dynamics at original image size (will be slower but create more accurate boundaries)

            interp: bool (optional, default True)
                interpolate during 2D dynamics (not available in 3D) 
                (in previous versions it was False)

            flow_threshold: float (optional, default 0.4)
                flow error threshold (all cells with errors below threshold are kept) (not used for 3D)

            mask_threshold: float (optional, default 0.0)
                all pixels with value above threshold kept for masks, decrease to find more and larger masks

            dist_threshold: float (optional, default None) DEPRECATED
                use mask_threshold instead

            cellprob_threshold: float (optional, default None) DEPRECATED
                use mask_threshold instead

            compute_masks: bool (optional, default True)
                Whether or not to compute dynamics and return masks.
                This is set to False when retrieving the styles for the size model.

            min_size: int (optional, default 15)
                minimum number of pixels per mask, can turn off with -1

            stitch_threshold: float (optional, default 0.0)
                if stitch_threshold>0.0 and not do_3D, masks are stitched in 3D to return volume segmentation

            progress: pyqt progress bar (optional, default None)
                to return progress bar status to GUI
                
            omni: bool (optional, default False)
                use omnipose mask recontruction features
            
            calc_trace: bool (optional, default False)
                calculate pixel traces and return as part of the flow
                
            verbose: bool (optional, default False)
                turn on additional output to logs for debugging 
            
            transparency: bool (optional, default False)
                modulate flow opacity by magnitude instead of brightness (can use flows on any color background) 
            
            loop_run: bool (optional, default False)
                internal variable for determining if model has been loaded, stops model loading in loop over images

            model_loaded: bool (optional, default False)
                internal variable for determining if model has been loaded, used in __main__.py

            Returns
            -------
            masks: list of 2D arrays, or single 3D array (if do_3D=True)
                labelled image, where 0=no masks; 1,2,...=mask labels

            flows: list of lists 2D arrays, or list of 3D arrays (if do_3D=True)
                flows[k][0] = XY flow in HSV 0-255
                flows[k][1] = flows at each pixel
                flows[k][2] = scalar cell probability (Cellpose) or distance transform (Omnipose)
                flows[k][3] = boundary output (nonempty for Omnipose)
                flows[k][4] = final pixel locations after Euler integration 
                flows[k][5] = pixel traces (nonempty for calc_trace=True)

            styles: list of 1D arrays of length 64, or single 1D array (if do_3D=True)
                style vector summarizing each image, also used to estimate size of objects in image

        """
        
        if cellprob_threshold is not None or dist_threshold is not None:
            mask_threshold = deprecation_warning_cellprob_dist_threshold(cellprob_threshold, dist_threshold)
        
        if verbose:
            models_logger.info('Evaluating with flow_threshold %0.2f, mask_threshold %0.2f'%(flow_threshold, mask_threshold))
            if omni:
                models_logger.info(f'using omni model, cluster {cluster}')
        
        
        if isinstance(x, list) or x.squeeze().ndim==5:
            masks, styles, flows = [], [], []
            tqdm_out = utils.TqdmToLogger(models_logger, level=logging.INFO)
            nimg = len(x)
            iterator = trange(nimg, file=tqdm_out) if nimg>1 else range(nimg)
            for i in iterator:
                maski, stylei, flowi = self.eval(x[i], 
                                                 batch_size=batch_size, 
                                                 channels= channels if channels is None else channels[i] if (len(channels)==len(x) and 
                                                                          (isinstance(channels[i], list) or isinstance(channels[i], np.ndarray)) and
                                                                          len(channels[i])==2) else channels, 
                                                 channel_axis=channel_axis, 
                                                 z_axis=z_axis, 
                                                 normalize=normalize, 
                                                 invert=invert,
                                                 rescale=rescale[i] if isinstance(rescale, list) or isinstance(rescale, np.ndarray) else rescale,
                                                 diameter=diameter[i] if isinstance(diameter, list) or isinstance(diameter, np.ndarray) else diameter, 
                                                 do_3D=do_3D, 
                                                 anisotropy=anisotropy, 
                                                 net_avg=net_avg, 
                                                 augment=augment, 
                                                 tile=tile, 
                                                 tile_overlap=tile_overlap,
                                                 resample=resample, 
                                                 interp=interp,
                                                 cluster=cluster,
                                                 mask_threshold=mask_threshold, 
                                                 diam_threshold=diam_threshold,
                                                 flow_threshold=flow_threshold, 
                                                 flow_factor=flow_factor,
                                                 compute_masks=compute_masks, 
                                                 min_size=min_size, 
                                                 stitch_threshold=stitch_threshold, 
                                                 progress=progress,
                                                 omni=omni,
                                                 calc_trace=calc_trace, 
                                                 verbose=verbose,
                                                 transparency=transparency,
                                                 loop_run=(i>0),
                                                 model_loaded=model_loaded)
                masks.append(maski)
                flows.append(flowi)
                styles.append(stylei)
            return masks, styles, flows 
        
        else:
            if not model_loaded and (isinstance(self.pretrained_model, list) and not net_avg and not loop_run):
                
                # whether or not we are using dataparallel 
                if self.torch and self.gpu:
                    net = self.net.module
                else:
                    net = self.net
                    
                net.load_model(self.pretrained_model[0], cpu=(not self.gpu))
                if not self.torch:
                    net.collect_params().grad_req = 'null'

            x = transforms.convert_image(x, channels, channel_axis=channel_axis, z_axis=z_axis,
                                         do_3D=(do_3D or stitch_threshold>0), normalize=False, 
                                         invert=False, nchan=self.nchan, dim=self.dim, omni=omni)
            if x.ndim < self.dim+2: # we need nimg x dims x channels, so 2D has 4, 3D has 5, etc. 
                x = x[np.newaxis]
            
            self.batch_size = batch_size
            rescale = self.diam_mean / diameter if (rescale is None and (diameter is not None and diameter>0)) else rescale
            rescale = 1.0 if rescale is None else rescale
            
            masks, styles, dP, cellprob, p, bd, tr = self._run_cp(x, 
                                                          compute_masks=compute_masks,
                                                          normalize=normalize,
                                                          invert=invert,
                                                          rescale=rescale, 
                                                          net_avg=net_avg, 
                                                          resample=resample,
                                                          augment=augment, 
                                                          tile=tile, 
                                                          tile_overlap=tile_overlap,
                                                          mask_threshold=mask_threshold, 
                                                          diam_threshold=diam_threshold,
                                                          flow_threshold=flow_threshold,
                                                          flow_factor=flow_factor,
                                                          interp=interp,
                                                          cluster=cluster,
                                                          min_size=min_size, 
                                                          do_3D=do_3D, 
                                                          anisotropy=anisotropy,
                                                          stitch_threshold=stitch_threshold,
                                                          omni=omni,
                                                          calc_trace=calc_trace,
                                                          verbose=verbose)
            flows = [plot.dx_to_circ(dP,transparency=transparency), dP, cellprob, p, bd, tr]
            return masks, flows, styles

    def _run_cp(self, x, compute_masks=True, normalize=True, invert=False,
                rescale=1.0, net_avg=True, resample=True,
                augment=False, tile=True, tile_overlap=0.1,
                mask_threshold=0.0, diam_threshold=12., flow_threshold=0.4, flow_factor=5.0, min_size=15,
                interp=True, cluster=False, anisotropy=1.0, do_3D=False, stitch_threshold=0.0,
                omni=False, calc_trace=False, verbose=False):
        
        tic = time.time()
        shape = x.shape
        nimg = shape[0] 
        
        bd, tr = None, None
        if do_3D:
            img = np.asarray(x)
            if normalize or invert: # possibly make normalize a vector of upper-lower values  
                img = transforms.normalize_img(img, invert=invert, omni=omni)
            yf, styles = self._run_3D(img, rsz=rescale, anisotropy=anisotropy, 
                                      net_avg=net_avg, augment=augment, tile=tile,
                                      tile_overlap=tile_overlap)
            cellprob = np.sum([yf[k][2] for k in range(3)],axis=0)/3 if omni else np.sum([yf[k][2] for k in range(3)],axis=0)
            bd = np.sum([yf[k][3] for k in range(3)],axis=0)/3 if self.nclasses==4 else np.zeros_like(cellprob)
            dP = np.stack((yf[1][0] + yf[2][0], yf[0][0] + yf[2][1], yf[0][1] + yf[1][1]), axis=0) # (dZ, dY, dX)
            if omni:
                dP = np.stack([gaussian_filter(dP[a],sigma=1.5) for a in range(3)]) # remove some artifacts
                bd = gaussian_filter(bd,sigma=1.5)
                cellprob = gaussian_filter(cellprob,sigma=1.5)
                dP = dP/2 #should be averaging components 
            del yf
        else:
            tqdm_out = utils.TqdmToLogger(models_logger, level=logging.INFO)
            iterator = trange(nimg, file=tqdm_out) if nimg>1 else range(nimg)
            styles = np.zeros((nimg, self.nbase[-1]), np.float32)
            
            #indexing a little weird here due to channels being last now 
            if resample:
                s = tuple(shape[-(self.dim+1):-1])
            else:
                s = tuple(np.round(np.array(shape[-(self.dim+1):-1])*rescale).astype(int))
            
            dP = np.zeros((self.dim, nimg,)+s, np.float32)
            cellprob = np.zeros((nimg,)+s, np.float32)
            for i in iterator:
                img = np.asarray(x[i])
                if normalize or invert:
                    img = transforms.normalize_img(img, invert=invert, omni=omni)


                if rescale != 1.0:
                    # if self.dim>2:
                    #     print('WARNING, resample not updated for ND')
                    # img = transforms.resize_image(img, rsz=rescale)
                    
                    if img.ndim>self.dim: # then there is a channel axis, assume it is last here 
                        img = np.stack([zoom(img[...,k],rescale,order=3) for k in range(img.shape[-1])],axis=-1)
                    else:
                        img = zoom(img,rescale,order=1)
                yf, style = self._run_nets(img, net_avg=net_avg,
                                           augment=augment, tile=tile,
                                           tile_overlap=tile_overlap)
                
                # resample interpolates the network output to native resolution prior to running Euler integration
                # this means the masks will have no scaling artifacts. We could *upsample* by some factor to make
                # the clustering etc. work even better, but that is not implemented yet 
                if resample:
                    # ND version actually gives better results than CV2 in some places. 
                    yf = np.stack([zoom(yf[...,k], shape[1:1+self.dim]/np.array(yf.shape[:2]), order=1) for k in range(yf.shape[-1])],axis=-1)
                    
                    # scipy.ndimage.affine_transform(A, np.linalg.inv(M), output_shape=tyx,
                cellprob[i] = yf[...,self.dim] #scalar field always after the vector field output 
                order = (self.dim,)+tuple([k for k in range(self.dim)]) #(2,0,1)
                dP[:, i] = yf[...,:self.dim].transpose(order) 
                if self.nclasses >= 4:
                    if i==0:
                        bd = np.zeros_like(cellprob)
                    bd[i] = yf[...,self.dim+1]
                styles[i] = style
            del yf, style
        styles = styles.squeeze()
        
        
        net_time = time.time() - tic
        if nimg > 1:
            models_logger.info('network run in %2.2fs'%(net_time))

        if compute_masks:
            tic=time.time()
            niter = 200 if (do_3D and not resample) else (1 / rescale * 200)
            if do_3D:
                if not (omni and OMNI_INSTALLED):
                    # run cellpose compute_masks
                    masks, p, tr = dynamics.compute_masks(dP, cellprob, bd, niter=niter, resize=None, 
                                                          mask_threshold=mask_threshold,
                                                          diam_threshold=diam_threshold, 
                                                          flow_threshold=flow_threshold,
                                                          interp=interp, do_3D=do_3D, min_size=min_size, verbose=verbose,
                                                          use_gpu=False, device=torch.device('cpu'), nclasses=self.nclasses,
                                                          calc_trace=calc_trace)
                else:
                    # run omnipose compute_masks
                    masks, p, tr = my_omnipose.core.compute_masks(dP, cellprob, bd,
                                                               do_3D=do_3D,
                                                               niter=niter,
                                                               resize=None,
                                                               min_size=min_size, 
                                                               mask_threshold=mask_threshold,  
                                                               diam_threshold=diam_threshold,
                                                               flow_threshold=flow_threshold, 
                                                               flow_factor=flow_factor,      
                                                               interp=interp, 
                                                               cluster=cluster, 
                                                               calc_trace=calc_trace, 
                                                               verbose=verbose,
                                                               use_gpu=False, 
                                                               device=torch.device('cpu'), 
                                                               nclasses=self.nclasses, 
                                                               dim=self.dim)
            else:
                masks, p, tr = [], [], []
                resize = shape[-(self.dim+1):-1] if not resample else None 
                # print('compute masks 2',resize,shape,resample)
                for i in iterator:
                    if not (omni and OMNI_INSTALLED):
                        # run cellpose compute_masks
                        outputs = dynamics.compute_masks(dP[:,i], cellprob[i], niter=niter, mask_threshold=mask_threshold,
                                                         flow_threshold=flow_threshold, interp=interp, resize=resize, verbose=verbose,
                                                         use_gpu=False, device=torch.device('cpu'), nclasses=self.nclasses,
                                                         calc_trace=calc_trace)
                    else:
                        # run omnipose compute_masks
                        
                        # important: resampling means that pixels need to go farther to cluser together;
                        # niter should be determined by dist, first of all; it currently is already scaled for resampling, good! 
                        # dP needs to be scaled for magnitude to get pixels to move the same relative distance
                        # eps probably should be left the same if the above are changed 
                        # if resample:
                        #     print('rescale is',rescale,resize)
                            # dP[:,i] /= rescale this does nothign here since I normalize the flow anyway, have to pass in 
                        
                        bdi = bd[i] if bd is not None else None
                        outputs = my_omnipose.core.compute_masks(dP[:,i], cellprob[i], bdi, 
                                                              niter=niter, 
                                                              rescale=rescale, 
                                                              resize=resize,
                                                              min_size=min_size, 
                                                              mask_threshold=mask_threshold,   
                                                              diam_threshold=diam_threshold,
                                                              flow_threshold=flow_threshold, 
                                                              flow_factor=flow_factor,             
                                                              interp=interp, 
                                                              cluster=cluster, 
                                                              calc_trace=calc_trace, 
                                                              verbose=verbose,
                                                              use_gpu=False, 
                                                              device=torch.device('cpu'), 
                                                              nclasses=self.nclasses, 
                                                              dim=self.dim) 
                    masks.append(outputs[0])
                    p.append(outputs[1])
                    tr.append(outputs[2])
                
                masks = np.array(masks)
                p = np.array(p)
                tr = np.array(tr)

                if stitch_threshold > 0 and nimg > 1:
                    models_logger.info(f'stitching {nimg} planes using stitch_threshold={stitch_threshold:0.3f} to make 3D masks')
                    masks = utils.stitch3D(masks, stitch_threshold=stitch_threshold)
            
            flow_time = time.time() - tic
            if nimg > 1:
                models_logger.info('masks created in %2.2fs'%(flow_time))
            masks, dP, cellprob, p = masks.squeeze(), dP.squeeze(), cellprob.squeeze(), p.squeeze()
            bd = bd.squeeze() if bd is not None else bd
        else:
            masks, p , tr = np.zeros(0), np.zeros(0), np.zeros(0) #pass back zeros if not compute_masks
        return masks, styles, dP, cellprob, p, bd, tr

        
    def loss_fn(self, lbl, y):
        """ loss function between true labels lbl and prediction y """
        if self.omni and OMNI_INSTALLED:
             #loss function for omnipose field 
            loss = my_omnipose.core.loss(self, lbl, y)
        else: # original loss function 
            veci = 5. * self._to_device(lbl[:,1:])
            lbl  = self._to_device(lbl[:,0]>.5)
            loss = self.criterion(y[:,:2] , veci) 
            if self.torch:
                loss /= 2.
            loss2 = self.criterion2(y[:,2] , lbl)
            loss = loss + loss2
        return loss        


    def train(self, train_data, train_labels, train_files=None, 
              test_data=None, test_labels=None, test_files=None,
              channels=None, channel_axis=0, normalize=True, 
              save_path=None, save_every=100, save_each=False,
              learning_rate=0.2, n_epochs=500, momentum=0.9, SGD=True,
              weight_decay=0.00001, batch_size=8, nimg_per_epoch=None,
              rescale=True, min_train_masks=5, netstr=None, tyx=None):

        """ train network with images train_data 
        
            Parameters
            ------------------

            train_data: list of arrays (2D or 3D)
                images for training

            train_labels: list of arrays (2D or 3D)
                labels for train_data, where 0=no masks; 1,2,...=mask labels
                can include flows as additional images

            train_files: list of strings
                file names for images in train_data (to save flows for future runs)

            test_data: list of arrays (2D or 3D)
                images for testing

            test_labels: list of arrays (2D or 3D)
                labels for test_data, where 0=no masks; 1,2,...=mask labels; 
                can include flows as additional images
        
            test_files: list of strings
                file names for images in test_data (to save flows for future runs)

            channels: list of ints (default, None)
                channels to use for training

            normalize: bool (default, True)
                normalize data so 0.0=1st percentile and 1.0=99th percentile of image intensities in each channel

            save_path: string (default, None)
                where to save trained model, if None it is not saved

            save_every: int (default, 100)
                save network every [save_every] epochs

            learning_rate: float or list/np.ndarray (default, 0.2)
                learning rate for training, if list, must be same length as n_epochs

            n_epochs: int (default, 500)
                how many times to go through whole training set during training

            weight_decay: float (default, 0.00001)

            SGD: bool (default, True) 
                use SGD as optimization instead of RAdam

            batch_size: int (optional, default 8)
                number of 224x224 patches to run simultaneously on the GPU
                (can make smaller or bigger depending on GPU memory usage)

            nimg_per_epoch: int (optional, default None)
                minimum number of images to train on per epoch, 
                with a small training set (< 8 images) it may help to set to 8

            rescale: bool (default, True)
                whether or not to rescale images to diam_mean during training, 
                if True it assumes you will fit a size model after training or resize your images accordingly,
                if False it will try to train the model to be scale-invariant (works worse)

            min_train_masks: int (default, 5)
                minimum number of masks an image must have to use in training set

            netstr: str (default, None)
                name of network, otherwise saved with name as params + training start time

        """
        if rescale:
            models_logger.info(f'Training with rescale = {rescale:.2f}')
        train_data, train_labels, test_data, test_labels, run_test = transforms.reshape_train_test(train_data, train_labels,  
                                                                                                   test_data, test_labels,
                                                                                                   channels, channel_axis,
                                                                                                   normalize, 
                                                                                                   self.dim, self.omni)
        # check if train_labels have flows
        # if not, flows computed, returned with labels as train_flows[i][0]
        labels_to_flows = dynamics.labels_to_flows if not (self.omni and OMNI_INSTALLED) else my_omnipose.core.labels_to_flows
        
        # do not precompute flows for omnipose 
        if self.omni and OMNI_INSTALLED:
            models_logger.info('No precomuting flows with Omnipose. Computed during training.')
            
            train_labels = [my_omnipose.utils.format_labels(label) for label in train_labels]
            nmasks = np.array([label.max() for label in train_labels])

        else:
            train_labels = labels_to_flows(train_labels, files=train_files, use_gpu=self.gpu, device=self.device, dim=self.dim)
            nmasks = np.array([label[0].max() for label in train_labels])

        if run_test:
            test_labels = labels_to_flows(test_labels, files=test_files, use_gpu=self.gpu, device=self.device)
        else:
            test_labels = None

        nremove = (nmasks < min_train_masks).sum()
        if nremove > 0:
            models_logger.warning(f'{nremove} train images with number of masks less than min_train_masks ({min_train_masks}), removing from train set')
            ikeep = np.nonzero(nmasks >= min_train_masks)[0]
            train_data = [train_data[i] for i in ikeep]
            train_labels = [train_labels[i] for i in ikeep]

        if channels is None:
            models_logger.warning('channels is set to None, input must therefore have nchan channels (default is 2)')
        model_path = self._train_net(train_data, train_labels, 
                                     test_data=test_data, test_labels=test_labels,
                                     save_path=save_path, save_every=save_every, save_each=save_each,
                                     learning_rate=learning_rate, n_epochs=n_epochs, 
                                     momentum=momentum, weight_decay=weight_decay, 
                                     SGD=SGD, batch_size=batch_size, nimg_per_epoch=nimg_per_epoch, 
                                     rescale=rescale, netstr=netstr,tyx=tyx)
        self.pretrained_model = model_path
        return model_path

class SizeModel():
    """ linear regression model for determining the size of objects in image
        used to rescale before input to cp_model
        uses styles from cp_model

        Parameters
        -------------------

        cp_model: UnetModel or CellposeModel
            model from which to get styles

        device: mxnet device (optional, default mx.cpu())
            where cellpose model is saved (mx.gpu() or mx.cpu())

        pretrained_size: str
            path to pretrained size model
            
        omni: bool
            whether or not to use distance-based size metrics
            corresponding to 'omni' model 

    """
    def __init__(self, cp_model, device=None, pretrained_size=None, **kwargs):
        super(SizeModel, self).__init__(**kwargs)

        self.pretrained_size = pretrained_size
        self.cp = cp_model
        self.device = self.cp.device
        self.diam_mean = self.cp.diam_mean
        self.torch = self.cp.torch
        if pretrained_size is not None:
            self.params = np.load(self.pretrained_size, allow_pickle=True).item()
            self.diam_mean = self.params['diam_mean']
        if not hasattr(self.cp, 'pretrained_model'):
            error_message = 'no pretrained cellpose model specified, cannot compute size'
            models_logger.critical(error_message)
            raise ValueError(error_message)
        
    def eval(self, x, channels=None, channel_axis=None, 
             normalize=True, invert=False, augment=False, tile=True,
             batch_size=8, progress=None, interp=True, omni=False):
        """ use images x to produce style or use style input to predict size of objects in image

            Object size estimation is done in two steps:
            1. use a linear regression model to predict size from style in image
            2. resize image to predicted size and run CellposeModel to get output masks.
                Take the median object size of the predicted masks as the final predicted size.

            Parameters
            -------------------

            x: list or array of images
                can be list of 2D/3D images, or array of 2D/3D images

            channels: list (optional, default None)
                list of channels, either of length 2 or of length number of images by 2.
                First element of list is the channel to segment (0=grayscale, 1=red, 2=green, 3=blue).
                Second element of list is the optional nuclear channel (0=none, 1=red, 2=green, 3=blue).
                For instance, to segment grayscale images, input [0,0]. To segment images with cells
                in green and nuclei in blue, input [2,3]. To segment one grayscale image and one
                image with cells in green and nuclei in blue, input [[0,0], [2,3]].

            channel_axis: int (optional, default None)
                if None, channels dimension is attempted to be automatically determined

            normalize: bool (default, True)
                normalize data so 0.0=1st percentile and 1.0=99th percentile of image intensities in each channel

            invert: bool (optional, default False)
                invert image pixel intensity before running network

            augment: bool (optional, default False)
                tiles image with overlapping tiles and flips overlapped regions to augment

            tile: bool (optional, default True)
                tiles image to ensure GPU/CPU memory usage limited (recommended)

            progress: pyqt progress bar (optional, default None)
                to return progress bar status to GUI

            Returns
            -------
            diam: array, float
                final estimated diameters from images x or styles style after running both steps

            diam_style: array, float
                estimated diameters from style alone

        """
        
        if isinstance(x, list):
            diams, diams_style = [], []
            nimg = len(x)
            tqdm_out = utils.TqdmToLogger(models_logger, level=logging.INFO)
            iterator = trange(nimg, file=tqdm_out) if nimg>1 else range(nimg)
            for i in iterator:
                diam, diam_style = self.eval(x[i], 
                                             channels=channels[i] if (len(channels)==len(x) and 
                                                                     (isinstance(channels[i], list) or isinstance(channels[i], np.ndarray)) and
                                                                     len(channels[i])==2) else channels,
                                             channel_axis=channel_axis, 
                                             normalize=normalize, 
                                             invert=invert,
                                             augment=augment,
                                             tile=tile,
                                             batch_size=batch_size,
                                             progress=progress,
                                             omni=omni)
                diams.append(diam)
                diams_style.append(diam_style)

            return diams, diams_style

        if x.squeeze().ndim > 3:
            models_logger.warning('image is not 2D cannot compute diameter')
            return self.diam_mean, self.diam_mean

        styles = self.cp.eval(x, 
                              channels=channels, 
                              channel_axis=channel_axis, 
                              normalize=normalize, 
                              invert=invert, 
                              augment=augment, 
                              tile=tile,
                              batch_size=batch_size, 
                              net_avg=False,
                              resample=False,
                              compute_masks=False)[-1]

        diam_style = self._size_estimation(np.array(styles))
        diam_style = self.diam_mean if (diam_style==0 or np.isnan(diam_style)) else diam_style
        
        masks = self.cp.eval(x, 
                             channels=channels, 
                             channel_axis=channel_axis, 
                             normalize=normalize, 
                             invert=invert, 
                             augment=augment, 
                             tile=tile,
                             batch_size=batch_size, 
                             net_avg=False,
                             resample=False,
                             rescale =  self.diam_mean / diam_style if self.diam_mean>0 else 1, 
                             #flow_threshold=0,
                             diameter=None,
                             interp=False,
                             omni=omni)[0]
        
        # allow backwards compatibility to older scale metric
        diam = utils.diameters(masks,omni=omni)[0]
        if hasattr(self, 'model_type') and (self.model_type=='nuclei' or self.model_type=='cyto') and not self.torch and not omni:
            diam_style /= (np.pi**0.5)/2
            diam = self.diam_mean / ((np.pi**0.5)/2) if (diam==0 or np.isnan(diam)) else diam
        else:
            diam = self.diam_mean if (diam==0 or np.isnan(diam)) else diam
        return diam, diam_style

    def _size_estimation(self, style):
        """ linear regression from style to size 
        
            sizes were estimated using "diameters" from square estimates not circles; 
            therefore a conversion factor is included (to be removed)
        
        """
        szest = np.exp(self.params['A'] @ (style - self.params['smean']).T +
                        np.log(self.diam_mean) + self.params['ymean'])
        szest = np.maximum(5., szest)
        return szest

    ## Probably need channel axis here too
    def train(self, train_data, train_labels,
              test_data=None, test_labels=None,
              channels=None, normalize=True, 
              learning_rate=0.2, n_epochs=10, 
              l2_regularization=1.0, batch_size=8): # dim and omni set by the model
        """ train size model with images train_data to estimate linear model from styles to diameters
        
            Parameters
            ------------------

            train_data: list of arrays (2D or 3D)
                images for training

            train_labels: list of arrays (2D or 3D)
                labels for train_data, where 0=no masks; 1,2,...=mask labels
                can include flows as additional images

            channels: list of ints (default, None)
                channels to use for training

            normalize: bool (default, True)
                normalize data so 0.0=1st percentile and 1.0=99th percentile of image intensities in each channel

            n_epochs: int (default, 10)
                how many times to go through whole training set (taking random patches) for styles for diameter estimation

            l2_regularization: float (default, 1.0)
                regularize linear model from styles to diameters

            batch_size: int (optional, default 8)
                number of 224x224 patches to run simultaneously on the GPU
                (can make smaller or bigger depending on GPU memory usage)
        """
        batch_size /= 2 # reduce batch_size by factor of 2 to use larger tiles
        batch_size = int(max(1, batch_size))
        self.cp.batch_size = batch_size
        print('sizemodel',self.cp.dim,self.cp.omni)
        train_data, train_labels, test_data, test_labels, run_test = transforms.reshape_train_test(train_data, train_labels,
                                                                                                   test_data, test_labels,
                                                                                                   channels, normalize, self.cp.dim, self.cp.omni)
        if isinstance(self.cp.pretrained_model, list):
            cp_model_path = self.cp.pretrained_model[0]
            self.cp.net.load_model(cp_model_path, cpu=(not self.cp.gpu))
            if not self.torch:
                self.cp.net.collect_params().grad_req = 'null'
        else:
            cp_model_path = self.cp.pretrained_model
        
        diam_train = np.array([utils.diameters(lbl,omni=self.cp.omni)[0] for lbl in train_labels])
        if run_test: 
            diam_test = np.array([utils.diameters(lbl,omni=self.cp.omni)[0] for lbl in test_labels])
        
        # remove images with no masks
        for i in range(len(diam_train)):
            if diam_train[i]==0.0:
                del train_data[i]
                del train_labels[i]
        if run_test:
            for i in range(len(diam_test)):
                if diam_test[i]==0.0:
                    del test_data[i]
                    del test_labels[i]

        nimg = len(train_data)
        styles = np.zeros((n_epochs*nimg, 256), np.float32)
        diams = np.zeros((n_epochs*nimg,), np.float32)
        tic = time.time()
        for iepoch in range(n_epochs):
            iall = np.arange(0,nimg,1,int)
            for ibatch in range(0,nimg,batch_size):
                inds = iall[ibatch:ibatch+batch_size]
                #using the orignal rotate and resize just because mine apparently broke the feature that
                # you could either pass in flows or masks... will eventually fix and streamline 
                imgi,lbl,scale = transforms.original_random_rotate_and_resize([train_data[i] for i in inds],
                                                                              Y=[train_labels[i].astype(np.int16) for i in inds], 
                                                                              scale_range=1, xy=(512,512)) 

                feat = self.cp.network(imgi)[1]
                styles[inds+nimg*iepoch] = feat
                diams[inds+nimg*iepoch] = np.log(diam_train[inds]) - np.log(self.diam_mean) + np.log(scale)
            del feat
            models_logger.info('ran %d epochs in %0.3f sec'%(iepoch+1, time.time()-tic))

        # create model
        smean = styles.mean(axis=0)
        X = ((styles - smean).T).copy()
        ymean = diams.mean()
        y = diams - ymean

        A = np.linalg.solve(X@X.T + l2_regularization*np.eye(X.shape[0]), X @ y)
        ypred = A @ X
        models_logger.info('train correlation: %0.4f'%np.corrcoef(y, ypred)[0,1])
            
        if run_test:
            nimg_test = len(test_data)
            styles_test = np.zeros((nimg_test, 256), np.float32)
            for i in range(nimg_test):
                styles_test[i] = self.cp._run_net(test_data[i].transpose((1,2,0)))[1]
            diam_test_pred = np.exp(A @ (styles_test - smean).T + np.log(self.diam_mean) + ymean)
            diam_test_pred = np.maximum(5., diam_test_pred)
            models_logger.info('test correlation: %0.4f'%np.corrcoef(diam_test, diam_test_pred)[0,1])

        self.pretrained_size = cp_model_path+'_size.npy'
        self.params = {'A': A, 'smean': smean, 'diam_mean': self.diam_mean, 'ymean': ymean}
        np.save(self.pretrained_size, self.params)
        models_logger.info('model saved to '+self.pretrained_size)
        return self.params
