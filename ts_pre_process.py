import os
join = os.path.join
import tifffile as tif
from skimage import io
from skimage.color import rgb2gray
import numpy as np

source_path = "/home/DataSet/CellSeg/Train_Labeled/images/"
target_path = "/home/DataSet/Omnipose_rgb/"

img_path = source_path

img_names = sorted(os.listdir(img_path))

for img_name in img_names:
        
    if img_name.endswith('.tif') or img_name.endswith('.tiff'):
        img_data = tif.imread(join(img_path, img_name))
    else:
        img_data = io.imread(join(img_path, img_name))

    # Convert to 3Channel
    if len(img_data.shape) == 2:
            img_data = np.repeat(np.expand_dims(img_data, axis=-1), 3, axis=-1)
    elif len(img_data.shape) == 3 and img_data.shape[-1] > 3:
        img_data = img_data[:,:, :3]
    else:
        pass
    
    # Conver to Gray scale
    #img_data = rgb2gray(img_data)*255

    # Change axis
    if len(img_data.shape) == 3:
        img_data = img_data.transpose(2,0,1)
    
    io.imsave(join(target_path,img_name.split('.')[0]+'_img.tif'), img_data.astype(np.uint8), check_contrast=False)