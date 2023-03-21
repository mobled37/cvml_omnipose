import sys, os, argparse, glob, pathlib, time
import subprocess

import numpy as np
from natsort import natsorted
from tqdm import tqdm
from . import utils, models, io

from .models import MODEL_NAMES
    
try:
    from .gui import gui 
    GUI_ENABLED = True 
except ImportError as err:
    GUI_ERROR = err
    GUI_ENABLED = False
    GUI_IMPORT = True
except Exception as err:
    GUI_ENABLED = False
    GUI_ERROR = err
    GUI_IMPORT = False
    raise
    
import logging
logger = logging.getLogger(__name__)

def confirm_prompt(question):
    reply = None
    while reply not in ("", "y", "n"):
        reply = input(f"{question} (y/n): ").lower()
    return (reply in ("", "y"))

def install(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package])
    
def check_omni(logger,omni=False):
    if omni and not 'omnipose' not in sys.modules:
        logger.info('Omnipose features requested but not installed.')
        confirm = confirm_prompt('Install Omnipose?')
        if confirm:
            install('omnipose')
        else:
            logger.info('Omnipose not installed. Running with omni=False')
        return confirm
    
# settings re-grouped a bit
# added omni as a parameter
def main(omni_CLI=False):
    print("********************Custom Cellpose************************")
    parser = argparse.ArgumentParser(description='cellpose parameters')
    
    # settings for CPU vs GPU
    hardware_args = parser.add_argument_group("hardware arguments")
    hardware_args.add_argument('--use_gpu', action='store_true', help='use gpu if torch or mxnet with cuda installed')
    hardware_args.add_argument('--check_mkl', action='store_true', help='check if mkl working')
    hardware_args.add_argument('--mkldnn', action='store_true', help='for mxnet, force MXNET_SUBGRAPH_BACKEND = "MKLDNN"')
        
    # settings for locating and formatting images
    input_img_args = parser.add_argument_group("input image arguments")
    input_img_args.add_argument('--dir',
                        default=[], type=str, help='folder containing data to run or train on.')
    input_img_args.add_argument('--look_one_level_down', action='store_true', help='run processing on all subdirectories of current folder')
    input_img_args.add_argument('--mxnet', action='store_true', help='use mxnet')
    input_img_args.add_argument('--img_filter',
                        default=[], type=str, help='end string for images to run on')
    input_img_args.add_argument('--channel_axis',
                        default=None, type=int, help='axis of image which corresponds to image channels')
    input_img_args.add_argument('--z_axis',
                        default=None, type=int, help='axis of image which corresponds to Z dimension')
    input_img_args.add_argument('--chan',
                        default=0, type=int, help='channel to segment; 0: GRAY, 1: RED, 2: GREEN, 3: BLUE. Default: %(default)s')
    input_img_args.add_argument('--chan2',
                        default=0, type=int, help='nuclear channel (if cyto, optional); 0: NONE, 1: RED, 2: GREEN, 3: BLUE. Default: %(default)s')
    input_img_args.add_argument('--invert', action='store_true', help='invert grayscale channel')
    input_img_args.add_argument('--all_channels', action='store_true', help='use all channels in image if using own model and images with special channels')
    input_img_args.add_argument('--dim',
                                default=2, type=int, help='number of spatiotemporal dimensions of images (not counting channels). Default: %(default)s') ##
    
    # model settings 
    model_args = parser.add_argument_group("model arguments")
    model_args.add_argument('--pretrained_model', required=False, default='cyto', type=str, help='model to use')
    model_args.add_argument('--unet', required=False, default=0, type=int, help='run standard unet instead of cellpose flow output')
    model_args.add_argument('--nclasses',default=3, type=int, help='if running unet, choose 2 or 3; if training omni, choose 4; standard Cellpose uses 3')
    model_args.add_argument('--kernel_size',default=2, type=int, help='kernel size for maskpool. Starts at 2, higher means more aggressive downsampling.')
    model_args.add_argument('--pretrained_size', action='store_true', help='apply pretrained size estimation model')

    # algorithm settings
    algorithm_args = parser.add_argument_group("algorithm arguments")
    algorithm_args.add_argument('--omni', action='store_true', help='Omnipose algorithm (disabled by default)')
    algorithm_args.add_argument('--cluster', action='store_true', help='DBSCAN clustering. Reduces oversegmentation of thin features (disabled by default).')
    algorithm_args.add_argument('--fast_mode', action='store_true', help='make code run faster by turning off 4 network averaging and resampling')
    algorithm_args.add_argument('--no_resample', action='store_true', help="disable dynamics on full image (makes algorithm faster for images with large diameters)")
    algorithm_args.add_argument('--no_net_avg', action='store_true', help='make code run faster by only running 1 network')
    algorithm_args.add_argument('--no_interp', action='store_true', help='do not interpolate when running dynamics (was default)')
    algorithm_args.add_argument('--do_3D', action='store_true', help='process images as 3D stacks of images (nplanes x nchan x Ly x Lx')
    algorithm_args.add_argument('--diameter', required=False, default=30., type=float, 
                                help='cell diameter, if 0 cellpose will estimate for each image')
    algorithm_args.add_argument('--stitch_threshold', required=False, default=0.0, type=float, help='compute masks in 2D then stitch together masks with IoU>0.9 across planes')
    algorithm_args.add_argument('--flow_threshold', default=0.4, type=float, help='flow error threshold, 0 turns off this optional QC step. Default: %(default)s')
    algorithm_args.add_argument('--mask_threshold', default=0, type=float, help='mask threshold, default is 0, decrease to find more and larger masks')
    algorithm_args.add_argument('--anisotropy', required=False, default=1.0, type=float,
                                help='anisotropy of volume in 3D')
    algorithm_args.add_argument('--diam_threshold', required=False, default=12.0, type=float, 
                                help='cell diameter threshold for upscaling before mask rescontruction, default 12.')
    algorithm_args.add_argument('--exclude_on_edges', action='store_true', help='discard masks which touch edges of image')
    
    # output settings
    output_args = parser.add_argument_group("output arguments")
    output_args.add_argument('--save_png', action='store_true', help='save masks as png')
    output_args.add_argument('--save_tif', action='store_true', help='save masks as tif')
    output_args.add_argument('--no_npy', action='store_true', help='suppress saving of npy')
    output_args.add_argument('--savedir',
                        default=None, type=str, help='folder to which segmentation results will be saved (defaults to input image directory)')
    output_args.add_argument('--dir_above', action='store_true', help='save output folders adjacent to image folder instead of inside it (off by default)')
    output_args.add_argument('--in_folders', action='store_true', help='flag to save output in folders (off by default)')
    output_args.add_argument('--save_flows', action='store_true', help='whether or not to save RGB images of flows when masks are saved (disabled by default)')
    output_args.add_argument('--save_outlines', action='store_true', help='whether or not to save RGB outline images when masks are saved (disabled by default)')
    output_args.add_argument('--save_ncolor', action='store_true', help='whether or not to save minimal "n-color" masks (disabled by default')
    output_args.add_argument('--save_txt', action='store_true', help='flag to enable txt outlines for ImageJ (disabled by default)')
    output_args.add_argument('--transparency', action='store_true', help='store flows with background transparent (alpha=flow magnitude) (disabled by default)')
    

    # training settings
    training_args = parser.add_argument_group("training arguments")
    training_args.add_argument('--train', action='store_true', help='train network using images in dir')
    training_args.add_argument('--train_size', action='store_true', help='train size network at end of training')
    training_args.add_argument('--mask_filter',
                        default='_masks', type=str, help='end string for masks to run on. Default: %(default)s')
    training_args.add_argument('--test_dir',
                        default=[], type=str, help='folder containing test data (optional)')
    training_args.add_argument('--learning_rate',
                        default=0.2, type=float, help='learning rate. Default: %(default)s')
    training_args.add_argument('--n_epochs',
                        default=500, type=int, help='number of epochs. Default: %(default)s')
    training_args.add_argument('--batch_size',
                        default=8, type=int, help='batch size. Default: %(default)s')
    training_args.add_argument('--min_train_masks',
                        default=1, type=int, help='minimum number of masks a training image must have to be used. Default: %(default)s')
    training_args.add_argument('--residual_on',
                        default=1, type=int, help='use residual connections')
    training_args.add_argument('--style_on',
                        default=1, type=int, help='use style vector')
    training_args.add_argument('--concatenation',
                        default=0, type=int, help='concatenate downsampled layers with upsampled layers (off by default which means they are added)')
    training_args.add_argument('--save_every',
                        default=100, type=int, help='number of epochs to skip between saves. Default: %(default)s')
    training_args.add_argument('--save_each', action='store_true', help='save the model under a different filename per --save_every epoch for later comparsion')
    training_args.add_argument('--RAdam', action='store_true', help='use RAdam instead of SGD')
    training_args.add_argument('--checkpoint', action='store_true', help='turn on checkpoints to reduce memeory usage')
    training_args.add_argument('--dropout',action='store_true', help='Use dropoint in training')
    training_args.add_argument('--tyx',
                        default=None, type=str, help='list of yx, zyx, or tyx dimensions for training')
    
    # misc settings
    parser.add_argument('--verbose', action='store_true', help='flag to output extra information (e.g. diameter metrics) for debugging and fine-tuning parameters')
    parser.add_argument('--testing', action='store_true', help='flag to suppress CLI user confirmation for saving output; for test scripts')
    
    args = parser.parse_args()

    # convert the tyx string to a vector
    if args.tyx is not None:
        args.tyx = tuple([int(s) for s in (args.tyx.split(','))])
    
    # handle mxnet option 
    if args.check_mkl:
        mkl_enabled = models.check_mkl((not args.mxnet))
    else:
        mkl_enabled = True

    if not args.train and (mkl_enabled and args.mkldnn):
        os.environ["MXNET_SUBGRAPH_BACKEND"]="MKLDNN"
    else:
        os.environ["MXNET_SUBGRAPH_BACKEND"]=""
    
    if len(args.dir)==0:
        if not GUI_ENABLED:
            print('GUI ERROR: %s'%GUI_ERROR)
            if GUI_IMPORT:
                print('GUI FAILED: GUI dependencies may not be installed, to install, run')
                print('     pip install cellpose[gui]')
        else:
            gui.run()

    else:
        if args.verbose:
            from .io import logger_setup
            logger, log_file = logger_setup(args.verbose)
            print('log file',log_file)
        else:
            print('!NEW LOGGING SETUP! To see cellpose progress, set --verbose')
            print('No --verbose => no progress or info printed')
            logger = logging.getLogger(__name__)

        use_gpu = False
        channels = [args.chan, args.chan2]

        # find images
        if len(args.img_filter)>0:
            img_filter = args.img_filter
        else:
            img_filter = ''


        # Check with user if they REALLY mean to run without saving anything 
        if not (args.train or args.train_size):
            saving_something = args.save_png or args.save_tif or args.save_flows or args.save_ncolor or args.save_txt
            if not (saving_something or args.testing): 
                print('Running without saving any output.')
                confirm = confirm_prompt('Proceed Anyway?')
                if not confirm:
                    exit()
                    
        device, gpu = models.assign_device((not args.mxnet), args.use_gpu)
        
        #define available model names, right now we have three broad categories 
        builtin_model = np.any([args.pretrained_model==s for s in MODEL_NAMES])
        cytoplasmic = 'cyto' in args.pretrained_model
        nuclear = 'nuclei' in args.pretrained_model
        #inelegant but necessary workaround for models that I provide without multiple models
        # long term should just check to see if they exist locally or on the server, disable model averaging if not found 
        bacterial = ('bact' in args.pretrained_model) or ('worm' in args.pretrained_model) 
        
        # force omni on for those models, but don't toggle it off if manually specified via --omni or by invoking python -m omnipose
        if 'omni' in args.pretrained_model or omni_CLI:
            args.omni = True
        
        if args.cluster and 'sklearn' not in sys.modules:
            print('DBSCAN clustering requires scikit-learn.')
            confirm = confirm_prompt('Install scikit-learn?')
            if confirm:
                install('scikit-learn')
            else:
                print('scikit-learn not installed. DBSCAN clustering will be automatically disabled.')
                          
        omni = check_omni(args.omni) # repeat the above check but factor it for use elsewhere
        if args.omni:
            print('Omnipose enabled. See Omnipose repo for licencing details.')
        

        # omni changes not implemented for mxnet. Full parity for cpu/gpu in pytorch. 
        if args.omni and args.mxnet:
            logger.info('omni only implemented in pytorch.')
            confirm = confirm_prompt('Continue with omni set to false?')
            if not confirm:
                exit()
            else:
                logger.info('omni set to false.')
                args.omni = False

        # # For now, omni version is not compatible with 3D. WIP. <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
        # if args.omni and args.do_3D:
        #     logger.info('omni not yet compatible with 3D segmentation.')
        #     confirm = confirm_prompt('Continue with omni set to false?')
        #     if not confirm:
        #         exit()
        #     else:
        #         logger.info('omni set to false.')
        #         args.omni = False

        # omni model needs 4 classes, but all the training regenerates this from scratch and just ignores saved CP flows. 
        if args.omni and args.train:
            logger.info('Training omni model. Setting nclasses=4, RAdam=True')
            args.nclasses = 4
            # args.dropout = True
            args.RAdam = True


        # EVALUATION BRANCH
        if not args.train and not args.train_size:
            tic = time.time()
            if not builtin_model:
                cpmodel_path = args.pretrained_model
                if not os.path.exists(cpmodel_path):
                    logger.warning('model path does not exist, using cyto model')
                    args.pretrained_model = 'cyto'
                else:
                    logger.info(f'running model {cpmodel_path}')

            image_names = io.get_image_files(args.dir, 
                                             args.mask_filter, 
                                             img_filter=img_filter,
                                             look_one_level_down=args.look_one_level_down)
            nimg = len(image_names)
                
            cstr0 = ['GRAY', 'RED', 'GREEN', 'BLUE']
            cstr1 = ['NONE', 'RED', 'GREEN', 'BLUE']
            logger.info('running cellpose on %d images using chan_to_seg %s and chan (opt) %s'%
                            (nimg, cstr0[channels[0]], cstr1[channels[1]]))
            if args.omni:
                logger.info(f'omni is ON, cluster is {args.cluster}')
             
            # handle built-in model exceptions
            if builtin_model:
                if args.mxnet:
                    if args.pretrained_model=='cyto2':
                        logger.warning('cyto2 model not available in mxnet, using cyto model')
                        args.pretrained_model = 'cyto'
                    if args.pretrained_model in OMNI_MODELS:
                        logger.warning('omnipose models not available in mxnet, using pytorch')
                        args.mxnet = False
                if not bacterial:              
                    model = models.Cellpose(gpu=gpu, device=device, model_type=args.pretrained_model, 
                                            torch=(not args.mxnet), omni=args.omni, 
                                            net_avg=(not args.fast_mode and not args.no_net_avg))
                else:
                    cpmodel_path = models.model_path(args.pretrained_model, 0, True)
                    model = models.CellposeModel(gpu=gpu, device=device, 
                                                 pretrained_model=cpmodel_path,
                                                 torch=True,
                                                 nclasses=args.nclasses, dim=args.dim, omni=args.omni,
                                                 net_avg=False)
            else:
                if args.chan > 0:
                    logger.info('Custom Model based on Cyto')
                    size_path = cpmodel_path + "_size.npy" if args.pretrained_size else None
                    model = models.Cellpose(gpu=gpu, device=device, model_type="cyto2_omni", 
                                            pretrained_model= cpmodel_path,
                                            pretrained_size = size_path,
                                            torch=(not args.mxnet), omni=args.omni,
                                            net_avg=(not args.fast_mode and not args.no_net_avg))
                else:
                    nchan = 2
                    if args.all_channels:
                        channels = None
                        nchan = 3
                    model = models.CellposeModel(gpu=gpu, device=device, 
                                                pretrained_model=cpmodel_path,
                                                torch=True,
                                                nclasses=args.nclasses, dim=args.dim, omni=args.omni,nchan=nchan,
                                                net_avg=False)
            

            # handle diameters
            if args.diameter==0:
                if builtin_model:
                    diameter = None
                    logger.info('estimating diameter for each image')
                else:
                    logger.info('using user-specified model, no auto-diameter estimation available')
                    diameter = model.diam_mean
                    if isinstance(model,models.Cellpose): diameter = None
            else:
                diameter = args.diameter
                logger.info('using diameter %0.2f for all images'%diameter)

            
            
            tqdm_out = utils.TqdmToLogger(logger,level=logging.INFO)
            
            for image_name in tqdm(image_names, file=tqdm_out):
                image = io.imread(image_name)
                out = model.eval(image, channels=channels, diameter=diameter,
                                do_3D=args.do_3D, net_avg=(not args.fast_mode and not args.no_net_avg),
                                augment=False,
                                resample=(not args.no_resample and not args.fast_mode),
                                flow_threshold=args.flow_threshold,
                                mask_threshold=args.mask_threshold,
                                diam_threshold=args.diam_threshold,
                                invert=args.invert,
                                batch_size=args.batch_size,
                                interp=(not args.no_interp),
                                cluster=args.cluster,
                                channel_axis=args.channel_axis,
                                z_axis=args.z_axis,
                                omni=args.omni,
                                anisotropy=args.anisotropy,
                                verbose=args.verbose,
                                transparency=args.transparency, # RGB flows made in the eval step
                                model_loaded=True)
                masks, flows = out[:2]
                if len(out) > 3:
                    diams = out[-1]
                else:
                    diams = diameter
                if args.exclude_on_edges:
                    masks = utils.remove_edge_masks(masks)
                if not args.no_npy:
                    io.masks_flows_to_seg(image, masks, flows, diams, image_name, channels)
                if saving_something:
                    io.save_masks(image, masks, flows, image_name, png=args.save_png, tif=args.save_tif,
                                  save_flows=args.save_flows,save_outlines=args.save_outlines,
                                  save_ncolor=args.save_ncolor,dir_above=args.dir_above,savedir=args.savedir,
                                  save_txt=args.save_txt,in_folders=args.in_folders)
            logger.info('completed in %0.3f sec'%(time.time()-tic))
            
        # TRAINING BRANCH    
        else:
            if builtin_model:
                if args.mxnet and args.pretrained_model=='cyto2':
                    logger.warning('cyto2 model not available in mxnet, using cyto model')
                    args.pretrained_model = 'cyto'
                cpmodel_path = models.model_path(args.pretrained_model, 0, not args.mxnet)
                if cytoplasmic:
                    szmean = 30.
                elif nuclear:
                    szmean = 17.
                elif bacterial:
                    szmean = 0. #bacterial models are not rescaled 
            else:
                cpmodel_path = os.fspath(args.pretrained_model)
                # szmean = 30.
                szmean = args.diameter # respect user defined, defaults to 30
                
            test_dir = None if len(args.test_dir)==0 else args.test_dir
            output = io.load_train_test_data(args.dir, test_dir, img_filter, args.mask_filter, args.unet, args.look_one_level_down, args.omni)
            images, labels, image_names, test_images, test_labels, image_names_test = output

            # training with all channels
            if args.all_channels:
                img = images[0] # but doesn't this only give the first channel?
                dim = img.ndim 
               
                shape = img.shape
                if args.dim != dim: # user dim allows us to detect 3D images with or with out channel axis
                    if args.channel_axis is not None:
                        nchan = shape[args.channel_axis]
                    else:
                        nchan = min(shape) # This assumes that the channel axis is the smallest 
                        args.channel_axis = np.where([s==nchan for s in shape])
                        logger.info('channel axis detected at position %s, manually specify if incorrect'%args.channel_axis)
                else: 
                    nchan = 1
                channels = None 
            else:
                nchan = 2

            
            # model path
            if not os.path.exists(cpmodel_path):
                if not args.train:
                    error_message = 'ERROR: model path missing or incorrect - cannot train size model'
                    logger.critical(error_message)
                    raise ValueError(error_message)
                cpmodel_path = False
                logger.info('training from scratch')
                if args.diameter==0:
                    rescale = False 
                    logger.info('median diameter set to 0 => no rescaling during training')
                else:
                    rescale = True
                    szmean = args.diameter 
            else:
                rescale = True
                if args.pretrained_model == 'bact_phase_omni': rescale = False
                args.diameter = szmean 
                logger.info('pretrained model %s is being used'%cpmodel_path)
                args.residual_on = 1
                args.style_on = 1
                args.concatenation = 0
            if rescale and args.train:
                logger.info('during training rescaling images to fixed diameter of %0.1f pixels'%args.diameter)
                
            # initialize model
            if args.unet:
                model = core.UnetModel(device=device,
                                        pretrained_model=cpmodel_path, 
                                        diam_mean=szmean,
                                        residual_on=args.residual_on,
                                        style_on=args.style_on,
                                        concatenation=args.concatenation,
                                        nclasses=args.nclasses,
                                        nchan=nchan)
            else:
                model = models.CellposeModel(device=device,
                                             gpu=gpu, # why was this not being passed in befrore?
                                             torch=(not args.mxnet),
                                             pretrained_model=cpmodel_path,
                                             diam_mean=szmean,
                                             residual_on=args.residual_on,
                                             style_on=args.style_on,
                                             concatenation=args.concatenation,
                                             nchan=nchan,
                                             nclasses=args.nclasses,
                                             dim=args.dim, # init to 2D pr 3D
                                             omni=args.omni,
                                             checkpoint=args.checkpoint,
                                             dropout=args.dropout,
                                             kernel_size=args.kernel_size) 
            
            # allow multiple GPUs, maybe wrap in test to see if there are multiple GPUs
            # model = nn.DataParallel(model)

            # train segmentation model
            if args.train:
                cpmodel_path = model.train(images, labels, train_files=image_names,
                                           test_data=test_images, test_labels=test_labels, test_files=image_names_test,
                                           learning_rate=args.learning_rate, channels=channels,
                                           save_path=os.path.realpath(args.dir), save_every=args.save_every,
                                           save_each=args.save_each,
                                           rescale=rescale,n_epochs=args.n_epochs,
                                           batch_size=args.batch_size, 
                                           min_train_masks=args.min_train_masks,
                                           SGD=(not args.RAdam),
                                           tyx=args.tyx)
                model.pretrained_model = cpmodel_path
                logger.info('model trained and saved to %s'%cpmodel_path)

            output = None
            images = None
            labels = None
            image_names = None
            test_images = None
            test_labels = None
            image_names_test = None
            # train size model
            if args.train_size:
                output = io.load_train_test_data(args.dir, test_dir, img_filter, args.mask_filter, args.unet, args.look_one_level_down, args.omni)
                images, labels, image_names, test_images, test_labels, image_names_test = output
                sz_model = models.SizeModel(cp_model=model, device=device)
                sz_model.train(images, labels, test_images, test_labels, channels=channels, batch_size=args.batch_size)
                if test_images is not None:
                    predicted_diams, diams_style = sz_model.eval(test_images, channels=channels)
                    if test_labels[0].ndim>2:
                        tlabels = [lbl[0] for lbl in test_labels]
                    else:
                        tlabels = test_labels 
                    ccs = np.corrcoef(diams_style, np.array([utils.diameters(lbl)[0] for lbl in tlabels]))[0,1]
                    cc = np.corrcoef(predicted_diams, np.array([utils.diameters(lbl)[0] for lbl in tlabels]))[0,1]
                    logger.info('style test correlation: %0.4f; final test correlation: %0.4f'%(ccs,cc))
                    np.save(os.path.join(args.test_dir, '%s_predicted_diams.npy'%os.path.split(cpmodel_path)[1]), 
                            {'predicted_diams': predicted_diams, 'diams_style': diams_style})

if __name__ == '__main__':
    main()
    
