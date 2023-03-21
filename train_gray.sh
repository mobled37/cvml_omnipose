python -m my_omnipose --train --use_gpu --dir /home/DataSet/Omnipose_gray --mask_filter _masks --n_epochs 4000 \
--pretrained_model bact_phase_omni --learning_rate 0.001 --diameter 0 --batch_size 8 --RAdam --img_filter _img \
--save_every 1 --min_train_masks 1