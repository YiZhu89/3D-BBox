import os
import argparse
import torch
from torch.utils.data import DataLoader
from miscs import config_utils as cu, eval_utils as eu, train_utils as tu, X_Logger
from datasets.kitti import KittiBoxSet
import models
from models.builder import build_from, build_loss
from datasets import box_label2tensor, box_image2input

parser = argparse.ArgumentParser()
parser.add_argument('-c', '--cfg_file', type=str, required=True, help='Config file path, required flag.')
parser.add_argument('-l', '--log_dir', type=str, help='Folder to save experiment records.')
parser.add_argument('--kitti_root', type=str, help='KITTI dataset root.')
parser.add_argument('--batch_size', type=int, help='Mini-batch size')
parser.add_argument('--linear_lr', action='store_true', help='Adjust learning rate linearly according to batch size.')
parser.add_argument('--num_workers', type=int, help='Number of workers for DataLoader')
FLAGS = parser.parse_args()

# parse configs
cfg_dict = cu.file2dict(FLAGS.cfg_file)
dataset_cfg = cu.parse_args_update(FLAGS, cfg_dict['dataset_cfg'].copy())
model_cfg = cfg_dict['model_cfg'].copy()
loss_cfg = cfg_dict['loss_cfg'].copy()
training_cfg = cfg_dict['training_cfg'].copy()
loader_cfg = cu.parse_args_update(FLAGS, training_cfg['loader_cfg'].copy())
optimizer_cfg = cu.parse_args_update(FLAGS, training_cfg['optimizer_cfg'].copy())
log_cfg = cu.parse_args_update(FLAGS, cfg_dict['log_cfg'].copy())

# build logger and backup configs
logger = X_Logger(log_cfg['log_dir'])
logger.add_parse_args(FLAGS)
logger.add_config_file(FLAGS.cfg_file)

# build dataset and dataloader
train_set = KittiBoxSet(kitti_root=dataset_cfg['kitti_root'], split='train', 
                        transform=box_image2input(**dataset_cfg['img_norm']), 
                        label_transform=box_label2tensor(dataset_cfg['del_labels']),
                        augment=dataset_cfg['augment'], augment_type=dataset_cfg['augment_type'])

train_loader = DataLoader(train_set, shuffle=True, **loader_cfg)

total_train_sample = len(train_loader) * train_loader.batch_size if train_loader.drop_last else len(train_loader.dataset)

val_set = KittiBoxSet(kitti_root=dataset_cfg['kitti_root'], split='val', 
                      transform=box_image2input(**dataset_cfg['img_norm']), 
                      label_transform=box_label2tensor(dataset_cfg['del_labels']))

val_loader = DataLoader(val_set, shuffle=True, **loader_cfg)

total_val_sample = len(val_loader) * val_loader.batch_size if val_loader.drop_last else len(val_loader.dataset)

# build model
posenet = build_from(models, model_cfg).cuda()

# build loss and loss weight scheduler
dimension_loss = build_loss(loss_cfg['dimension_loss_cfg'].copy()).cuda()
pose_loss = build_loss(loss_cfg['pose_loss_cfg'].copy()).cuda()
dim_reg_wt = tu.loss_weight_scheduler(**loss_cfg['loss_weights']['dim_reg'])
bin_conf_wt = tu.loss_weight_scheduler(**loss_cfg['loss_weights']['bin_conf'])
bin_reg_wt = tu.loss_weight_scheduler(**loss_cfg['loss_weights']['bin_reg'])

# build optimizer
optim_type = getattr(torch.optim, optimizer_cfg.pop('type'))
if FLAGS.linear_lr:
    optimizer_cfg['lr'] *= train_loader.batch_size / training_cfg['loader_cfg']['batch_size']
optimizer = optim_type(posenet.parameters(), **optimizer_cfg)

# build predictor
dimension_predictor = eu.Dimension_Predictor(loss_cfg['dimension_loss_cfg']['avg_dim']).cuda()
pose_predictor = eu.Pose_Predictor(loss_cfg['pose_loss_cfg']['num_bins']).cuda()

# begin training
logger.info('TRAINING BEGINS!!!')
iteration = 0
for epoch in range(training_cfg['total_epoch']):

    logger.info('******** EPOCH %03d ********' % (epoch + 1))

    for batch_image, batch_label in train_loader:

        # load batch data to gpu
        batch_image_cuda = batch_image.cuda()
        batch_dim_label_cuda = batch_label['dimensions'].cuda()
        batch_theta_l_label_cuda = batch_label['theta_l'].cuda()

        # forward
        optimizer.zero_grad()
        dim_reg, bin_conf, bin_reg = posenet(batch_image_cuda)

        # loss
        dim_reg_loss = dimension_loss(dim_reg, batch_dim_label_cuda, reduction='batch_mean')
        bin_conf_loss, bin_reg_loss = pose_loss(bin_conf, bin_reg, batch_theta_l_label_cuda, reg_reduction='batch_mean')
        loss = dim_reg_loss * dim_reg_wt.get_loss_weight(iteration) +  \
               bin_conf_loss * bin_conf_wt.get_loss_weight(iteration) + \
               bin_reg_loss * bin_reg_wt.get_loss_weight(iteration)
        
        # optimize
        loss.backward()
        optimizer.step()

        # log
        if iteration % log_cfg['log_loss_every'] == 0:
            logger.add_scalar('Loss/dim_reg', dim_reg_loss.item(), iteration)
            logger.add_scalar('Loss/bin_conf', bin_conf_loss.item(), iteration)
            logger.add_scalar('Loss/bin_reg', bin_reg_loss.item(), iteration)
        
        if iteration % log_cfg['show_loss_every'] == 0:
            logger.info('batch %05d | dim_reg: %8.6f | bin_conf: %8.6f | bin_reg: %9.6f' \
                        % (iteration, dim_reg_loss.item(), bin_conf_loss.item(), bin_reg_loss.item()))

        iteration += 1
    
    # checkpoint
    if (epoch + 1) % log_cfg['ckpt_every'] == 0:
        logger.add_checkpoint(epoch + 1, posenet, optimizer)
    
    # eval
    if (epoch + 1) % log_cfg['eval_every'] == 0:
        logger.info('EVAL ...')
        posenet.eval()

        # eval train set
        dim_metric = {
            0.50: 0.0, 
            0.70: 0.0, 
            0.90: 0.0
        }
        bin_metric = {
            0.90: 0.0, 
            0.95: 0.0, 
            0.99: 0.0
        }
        train_loader.dataset.augment = False
        for batch_image, batch_label in train_loader:
            
            # load batch data to gpu
            batch_image_cuda = batch_image.cuda()
            batch_dim_label_cuda = batch_label['dimensions'].cuda()
            batch_theta_l_label_cuda = batch_label['theta_l'].cuda()

            # forward
            with torch.no_grad():
                dim_reg, bin_conf, bin_reg = posenet(batch_image_cuda)
            
            # predict
            _, dim_pred_score = dimension_predictor.predict_and_eval(dim_reg, batch_dim_label_cuda)
            _, bin_pred_score = pose_predictor.predict_and_eval(bin_conf, bin_reg, batch_theta_l_label_cuda)

            # accumulate statistics
            for key in dim_metric:
                dim_metric[key] += (dim_pred_score > key).sum().item()
            for key in bin_metric:
                bin_metric[key] += (bin_pred_score > key).sum().item()
        train_loader.dataset.augment = dataset_cfg['augment']
        
        output_str = 'TRAIN SET'
        
        for key in sorted(dim_metric.keys()):
            dim_metric[key] /= total_train_sample
            logger.add_scalar('EVAL_TRAIN/AIoU_3D_%4.2f'%key, dim_metric[key], epoch + 1)
            output_str += ' | A-IoU 3D @ %4.2f: %6.4f'%(key, dim_metric[key])

        for key in bin_metric:
            bin_metric[key] /= total_train_sample
            logger.add_scalar('EVAL_TRAIN/OS_%4.2f'%key, bin_metric[key], epoch + 1)
            output_str += ' | OS @ %4.2f: %6.4f'%(key, bin_metric[key])

        logger.info(output_str)
        
        # eval val set
        dim_metric = {
            0.50: 0.0, 
            0.70: 0.0, 
            0.90: 0.0
        }
        bin_metric = {
            0.90: 0.0, 
            0.95: 0.0, 
            0.99: 0.0
        }
        for batch_image, batch_label in val_loader:
            
            # load batch data to gpu
            batch_image_cuda = batch_image.cuda()
            batch_dim_label_cuda = batch_label['dimensions'].cuda()
            batch_theta_l_label_cuda = batch_label['theta_l'].cuda()

            # forward
            with torch.no_grad():
                dim_reg, bin_conf, bin_reg = posenet(batch_image_cuda)
            
            # predict
            _, dim_pred_score = dimension_predictor.predict_and_eval(dim_reg, batch_dim_label_cuda)
            _, bin_pred_score = pose_predictor.predict_and_eval(bin_conf, bin_reg, batch_theta_l_label_cuda)
            
            # accumulate statistics
            for key in dim_metric:
                dim_metric[key] += (dim_pred_score > key).sum().item()
            for key in bin_metric:
                bin_metric[key] += (bin_pred_score > key).sum().item()
        
        output_str = 'VALID SET'
        
        for key in sorted(dim_metric.keys()):
            dim_metric[key] /= total_val_sample
            logger.add_scalar('EVAL_VALID/AIoU_3D_%4.2f'%key, dim_metric[key], epoch + 1)
            output_str += ' | A-IoU 3D @ %4.2f: %6.4f'%(key, dim_metric[key])

        for key in bin_metric:
            bin_metric[key] /= total_val_sample
            logger.add_scalar('EVAL_VALID/OS_%4.2f'%key, bin_metric[key], epoch + 1)
            output_str += ' | OS @ %4.2f: %6.4f'%(key, bin_metric[key])

        logger.info(output_str)

        # eval val set
        posenet.train()
    
    if len(training_cfg['lr_decay_epochs']) > 0 and epoch + 1 == training_cfg['lr_decay_epochs'][0]:
        training_cfg['lr_decay_epochs'].pop(0)
        lr_decay_rate = training_cfg['lr_decay_rates'].pop(0)
        for param_group in optimizer.param_groups:
            param_group['lr'] *= lr_decay_rate

logger.info('TRAINING ENDS!!!')
logger.close()