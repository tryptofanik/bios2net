'''
    Single-GPU training.
    Will use H5 dataset in default. If using normal, will shift to the normal dataset.
'''
import argparse
import math
from datetime import datetime, timezone
import numpy as np
import tensorflow.compat.v1 as tf
tf.disable_v2_behavior()
import socket
import importlib
import os
import sys
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = BASE_DIR
sys.path.append(BASE_DIR)
sys.path.append(os.path.join(ROOT_DIR, 'models'))
sys.path.append(os.path.join(ROOT_DIR, 'utils'))
import provider
import tf_util
import fold_dataset
import matplotlib.pyplot as plt
import seaborn as sns


parser = argparse.ArgumentParser()
parser.add_argument('--dataset_path', type=str, help='Path to dataset')
parser.add_argument('--gpu', type=int, default=0, help='GPU to use [default: GPU 0]')
parser.add_argument('--model', default='bios2net', help='Model name [default: biossnet]')
parser.add_argument('--no_temporal', default=False, action='store_true')
parser.add_argument('--no_extractor', default=False, action='store_true')
parser.add_argument('--log_dir', default='log', help='Log dir [default: log]')
parser.add_argument('--num_point', type=int, default=1024, help='Point Number [default: 1024]')
parser.add_argument('--max_epoch', type=int, default=800, help='Epoch to run [default: 251]')
parser.add_argument('--batch_size', type=int, default=32, help='Batch Size during training [default: 32]')
parser.add_argument('--learning_rate', type=float, default=0.001, help='Initial learning rate [default: 0.001]')
parser.add_argument('--momentum', type=float, default=0.9, help='Initial learning rate [default: 0.9]')
parser.add_argument('--optimizer', default='adam', help='adam or momentum [default: adam]')
parser.add_argument('--weight_decay', default=0.001, type=float, help='Weight decay applied to all dense and conv layers.')
parser.add_argument('--shuffle_points', action='store_true', default=False, help='Whether to shuffle points within examples.')
parser.add_argument('--knn', action='store_true', default=False, help='Whether to use knn for point sampling')
parser.add_argument('--decay_step', type=int, default=180000, help='Decay step for lr decay [default: 200000]')
parser.add_argument('--decay_rate', type=float, default=0.7, help='Decay rate for lr decay [default: 0.7]')
parser.add_argument('--normal', action='store_true', help='Whether to use normal information')
parser.add_argument('--wandb', type=str, default=None, help='Project title on wandb')
parser.add_argument('--dont_add_n_c', action='store_true', default=False)
parser.add_argument('--to_categorical_index', nargs="+", type=int, default=[], help='Indicate which indices correspod to categorical values')
parser.add_argument('--to_categorical_sizes', nargs="+", type=int, default=[], help='Indicate sizes of subsequent categorical values')
parser.add_argument('--omit_parameters_ranges', nargs='+', type=int, default=[], help='Ranges of indices of parameters to omit in min, max order.')
parser.add_argument('--aux_losses', nargs='+', type=float, default=[0.5, 0.35, 0.15], help='Weights for auxilliary losses for main classification head, pointnet head and two temporal heads.')

FLAGS = parser.parse_args()


print('PARAMETERS:', FLAGS)

DATASET_PATH = FLAGS.dataset_path
BATCH_SIZE = FLAGS.batch_size
NUM_POINT = FLAGS.num_point
MAX_EPOCH = FLAGS.max_epoch
BASE_LEARNING_RATE = FLAGS.learning_rate
GPU_INDEX = FLAGS.gpu
MOMENTUM = FLAGS.momentum
OPTIMIZER = FLAGS.optimizer
EXTRACTOR = not FLAGS.no_extractor
TEMPORAL = not FLAGS.no_temporal
WEIGHT_DECAY = FLAGS.weight_decay
SHUFFLE_POINTS = FLAGS.shuffle_points
KNN = FLAGS.knn
DECAY_STEP = FLAGS.decay_step
DECAY_RATE = FLAGS.decay_rate
WANDB = FLAGS.wandb
ADD_N_C = not FLAGS.dont_add_n_c
TO_CATEGORICAL_IND = FLAGS.to_categorical_index
TO_CATEGORICAL_SIZES = FLAGS.to_categorical_sizes
OMIT_PARAMETERS_RANGES = FLAGS.omit_parameters_ranges
AUX_LOSSES = FLAGS.aux_losses

if len(OMIT_PARAMETERS_RANGES) % 2 != 0:
    raise Exception('You should provide even number for flag omit_parameters_range')

MODEL = importlib.import_module(FLAGS.model) # import network module
MODEL_FILE = os.path.join(ROOT_DIR, 'models', FLAGS.model+'.py')
LOG_DIR = FLAGS.log_dir
if not os.path.exists(LOG_DIR): os.mkdir(LOG_DIR)
os.system('cp %s %s' % (MODEL_FILE, LOG_DIR)) # bkp of model def
os.system('cp train.py %s' % (LOG_DIR)) # bkp of train procedure
LOG_FOUT = open(os.path.join(LOG_DIR, 'log_train.txt'), 'w')
LOG_FOUT.write(str(FLAGS)+'\n')

BN_INIT_DECAY = 0.5
BN_DECAY_DECAY_RATE = 0.5
BN_DECAY_DECAY_STEP = float(DECAY_STEP)
BN_DECAY_CLIP = 0.99

HOSTNAME = socket.gethostname()

TRAIN_DATASET = fold_dataset.PFRDataset(
    root=DATASET_PATH,
    batch_size=BATCH_SIZE,
    npoints=NUM_POINT,
    split='train',
    normalize=False,
    normal_channel=True,
    shuffle_points=SHUFFLE_POINTS,
    add_n_c_info=ADD_N_C,
    omit_parameters_ranges=OMIT_PARAMETERS_RANGES,
    to_categorical_indexes=TO_CATEGORICAL_IND,
    to_categorical_sizes=TO_CATEGORICAL_SIZES
)
TEST_DATASET = fold_dataset.PFRDataset(
    root=DATASET_PATH,
    batch_size=BATCH_SIZE,
    npoints=NUM_POINT,
    split='test',
    normalize=False,
    normal_channel=True,
    add_n_c_info=ADD_N_C,
    omit_parameters_ranges=OMIT_PARAMETERS_RANGES,
    to_categorical_indexes=TO_CATEGORICAL_IND,
    to_categorical_sizes=TO_CATEGORICAL_SIZES
)

assert len(TEST_DATASET.classes_names) == len(TRAIN_DATASET.classes_names)
NUM_CLASSES = len(TEST_DATASET.classes_names)
FEATURES_CHANNELS = TRAIN_DATASET.num_channel() - 3
LABELS = [i[0] for i in sorted(TRAIN_DATASET.classes.items(), key=lambda item: item[1])]

print(f'Database created with {FEATURES_CHANNELS + 3} channels')

def get_timestamp():
    timestamp = str(datetime.now(timezone.utc))[:16]
    timestamp = timestamp.replace('-', '')
    timestamp = timestamp.replace(' ', '_')
    timestamp = timestamp.replace(':', '')
    return timestamp

INIT_TIMESTAMP = get_timestamp()

if WANDB:
    import wandb
    wandb.init(
        project=WANDB, name=LOG_DIR if LOG_DIR else INIT_TIMESTAMP,
        tags=[
            DATASET_PATH.split('/')[-1],
            str(NUM_POINT),
            '-'.join([str(i) for i in OMIT_PARAMETERS_RANGES]),
            FLAGS.model,
            'extr' if EXTRACTOR else 'no_extr',
            'temporal' if TEMPORAL else 'no_temporal',
        ]
    )

def log_string(out_str):
    LOG_FOUT.write(out_str+'\n')
    LOG_FOUT.flush()
    print(out_str)

def plot_conf_matrix(confusion_matrix, normalize=True):
    plt.figure(figsize=(20, 19.5))  # width by height
    if normalize:
        col_norm = np.sum(confusion_matrix, axis=0)
        confusion_matrix /= col_norm[None, :]
    ax = sns.heatmap(confusion_matrix, #annot=True, annot_kws={'size': 3},
                    fmt='.1f', cbar=False, cmap='binary', linecolor='black', linewidths=0.5)
    return plt

def get_learning_rate(batch):
    learning_rate = tf.train.exponential_decay(
                        BASE_LEARNING_RATE,  # Base learning rate.
                        batch * BATCH_SIZE,  # Current index into the dataset.
                        DECAY_STEP,          # Decay step.
                        DECAY_RATE,          # Decay rate.
                        staircase=True)
    learning_rate = tf.maximum(learning_rate, 0.00001) # CLIP THE LEARNING RATE!
    return learning_rate

def get_bn_decay(batch):
    bn_momentum = tf.train.exponential_decay(
                      BN_INIT_DECAY,
                      batch*BATCH_SIZE,
                      BN_DECAY_DECAY_STEP,
                      BN_DECAY_DECAY_RATE,
                      staircase=True)
    bn_decay = tf.minimum(BN_DECAY_CLIP, 1 - bn_momentum)
    return bn_decay

def train():
    EPOCH_CNT = 0
    print('Start training...')
    with tf.Graph().as_default():
        print('tf.Graph created')
        with tf.device('/gpu:'+str(GPU_INDEX)):
            print('connected to gpu')
            print('model:', MODEL)
            pointclouds_pl, labels_pl, weights_pl = MODEL.placeholder_inputs(BATCH_SIZE, NUM_POINT, 3 + FEATURES_CHANNELS)
            is_training_pl = tf.placeholder(tf.bool, shape=())

            # Note the global_step=batch parameter to minimize.
            # That tells the optimizer to helpfully increment the 'batch' parameter
            # for you every time it trains.
            batch = tf.get_variable('batch', [],
                initializer=tf.constant_initializer(0), trainable=False)
            bn_decay = get_bn_decay(batch)
            tf.summary.scalar('bn_decay', bn_decay)

            # Get model and loss

            pred, site_preds, end_points = MODEL.get_model(pointclouds_pl, is_training_pl, NUM_CLASSES, bn_decay=bn_decay,
                                                          extractor=EXTRACTOR, temporal=TEMPORAL,
                                                          weight_decay=WEIGHT_DECAY, knn=KNN)
            print('site preds:', site_preds)
            MODEL.get_loss(pred, site_preds, labels_pl, end_points, AUX_LOSSES, weights_pl)
            losses = tf.get_collection('losses')
            total_loss = tf.add_n(losses, name='total_loss')
            tf.summary.scalar('total_loss', total_loss)
            for l in losses + [total_loss]:
                tf.summary.scalar(l.op.name, l)

            correct = tf.equal(tf.argmax(pred, 1), tf.to_int64(labels_pl))
            accuracy = tf.reduce_sum(tf.cast(correct, tf.float32)) / float(BATCH_SIZE)
            tf.summary.scalar('accuracy', accuracy)

            print("--- Get training operator")
            # Get training operator
            learning_rate = get_learning_rate(batch)
            tf.summary.scalar('learning_rate', learning_rate)
            if OPTIMIZER == 'momentum':
                optimizer = tf.train.MomentumOptimizer(learning_rate, momentum=MOMENTUM)
            elif OPTIMIZER == 'adam':
                optimizer = tf.train.AdamOptimizer(learning_rate)
            train_op = optimizer.minimize(total_loss, global_step=batch)

            # Add ops to save and restore all the variables.
            saver = tf.train.Saver()

        # Create a session
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        config.allow_soft_placement = True
        config.log_device_placement = False
        sess = tf.Session(config=config)

        # Add summary writers
        merged = tf.summary.merge_all()
        train_writer = tf.summary.FileWriter(os.path.join(LOG_DIR, 'train'), sess.graph)
        test_writer = tf.summary.FileWriter(os.path.join(LOG_DIR, 'test'), sess.graph)

        # Init variables
        init = tf.global_variables_initializer()
        sess.run(init)

        ops = {'pointclouds_pl': pointclouds_pl,
               'labels_pl': labels_pl,
               'weights_pl': weights_pl,
               'is_training_pl': is_training_pl,
               'pred': pred,
               'losses': losses,
               'loss': total_loss,
               'train_op': train_op,
               'merged': merged,
               'step': batch,
               'lr': learning_rate,
               'end_points': end_points}

        best_acc = -1
        best_epoch = -1

        for epoch in range(MAX_EPOCH):
            log_string(f'\n**** EPOCH {epoch:03d} ****')
            sys.stdout.flush()

            train_one_epoch(sess, ops, train_writer, EPOCH_CNT)
            if epoch % 2 == 0:
                acc = eval_one_epoch(sess, ops, test_writer, EPOCH_CNT)

                # Save the variables to disk.
                if acc > best_acc:
                    save_path = saver.save(sess, os.path.join(LOG_DIR, str(epoch), "model.ckpt"))
                    log_string(f"Model saved in file: {save_path}")
                    best_acc = acc
                    best_epoch = EPOCH_CNT
            if EPOCH_CNT - best_epoch > 120:
                log_string('Model didn\'t for 120 epoches. Aborting.')
                break
            EPOCH_CNT += 1


def train_one_epoch(sess, ops, train_writer, EPOCH_CNT):
    """ ops: dict mapping from string to tf ops """
    is_training = True

    log_string(str(datetime.now()))

    # Make sure batch data is of same size
    cur_batch_data = np.zeros((BATCH_SIZE,NUM_POINT,TRAIN_DATASET.num_channel()))
    cur_batch_label = np.zeros((BATCH_SIZE), dtype=np.int32)
    cur_batch_weights = np.zeros((BATCH_SIZE), dtype=np.float32)

    confusion_matrix = np.zeros((NUM_CLASSES, NUM_CLASSES))
    top3_correct = 0
    top3_class_correct = np.zeros((NUM_CLASSES))
    batch_losses = []
    batch_idx = 0
    while TRAIN_DATASET.has_next_batch():
        batch_data, batch_label, batch_cls_weights = TRAIN_DATASET.next_batch(augment=True)
        # batch_data = provider.random_point_dropout(batch_data)
        bsize = batch_data.shape[0]
        cur_batch_data[0:bsize,...] = batch_data
        cur_batch_label[0:bsize] = batch_label
        cur_batch_weights[0:bsize] = batch_cls_weights

        feed_dict = {ops['pointclouds_pl']: cur_batch_data,
                     ops['labels_pl']: cur_batch_label,
                     ops['is_training_pl']: is_training,
                     ops['weights_pl']: cur_batch_weights
                    }
        summary, step, _, loss_val, losses, pred_val, lr = sess.run([ops['merged'], ops['step'],
            ops['train_op'], ops['loss'], ops['losses'], ops['pred'], ops['lr']], feed_dict=feed_dict)

        train_writer.add_summary(summary, step)
        pred_val_arg = np.argmax(pred_val, 1)
        batch_losses.append(loss_val)

        for i in range(0, bsize):
            l = batch_label[i]
            top3 = pred_val[i].argsort()[-3:][::-1]
            top3_correct += l in top3
            top3_class_correct[l] += l in top3
            confusion_matrix[pred_val_arg[i], l] += 1

    col_norm = np.maximum(np.nan_to_num(np.sum(confusion_matrix, axis=0)), 1)
    accuracy = np.sum(np.diag(confusion_matrix)) / np.sum(confusion_matrix)
    top3_accuracy = top3_correct / np.sum(confusion_matrix)
    top3_avg_class_acc = np.mean(top3_class_correct / col_norm)
    class_acc = np.diag(confusion_matrix) / col_norm
    avg_class_acc = np.mean(np.nan_to_num(class_acc))
    log_string(f'mean loss: {np.mean(batch_losses)}')
    log_string(f'accuracy: {accuracy}')
    log_string(f'avg_class_acc: {avg_class_acc}')
    log_string(f'top3 acc: {top3_accuracy}')
    log_string(f'top3 avg_class_acc: {top3_avg_class_acc}')
    if WANDB:
        wandb.log(
            {'mean loss': np.mean(batch_losses),
            'accuracy': accuracy,
            'avg class acc': avg_class_acc,
            'top3 acc': top3_accuracy,
            'top3 avg class acc': top3_avg_class_acc,
#             'confusion matrix': wandb.Image(plot_conf_matrix(confusion_matrix, True)),
             'epoch': EPOCH_CNT,
             'lr': lr,
            },
            step=step
        )
        batch_idx += 1
    plt.close()
    TRAIN_DATASET.reset()

def eval_one_epoch(sess, ops, test_writer, EPOCH_CNT):
    """ ops: dict mapping from string to tf ops """
    is_training = False

    # Make sure batch data is of same size
    cur_batch_data = np.zeros((BATCH_SIZE,NUM_POINT,TEST_DATASET.num_channel()))
    cur_batch_label = np.zeros((BATCH_SIZE), dtype=np.int32)
    cur_batch_weights = np.zeros((BATCH_SIZE), dtype=np.float32)

    confusion_matrix = np.zeros((NUM_CLASSES, NUM_CLASSES))
    batch_losses = []
    top3_correct = 0
    top3_class_correct = np.zeros((NUM_CLASSES))
    batch_idx = 0

    log_string(str(datetime.now()))
    log_string(f'---- EPOCH {EPOCH_CNT:03d} EVALUATION ----')

    while TEST_DATASET.has_next_batch():
        batch_data, batch_label, batch_cls_weights = TEST_DATASET.next_batch(augment=False)
        bsize = batch_data.shape[0]
        # for the last batch in the epoch, the bsize:end are from last batch
        cur_batch_data[0:bsize,...] = batch_data
        cur_batch_label[0:bsize] = batch_label
        cur_batch_weights[0:bsize] = batch_cls_weights

        feed_dict = {ops['pointclouds_pl']: cur_batch_data,
                     ops['labels_pl']: cur_batch_label,
                     ops['is_training_pl']: is_training,
                     ops['weights_pl']: cur_batch_weights}
        summary, step, loss_val, losses, pred_val, lr = sess.run([ops['merged'], ops['step'],
            ops['loss'], ops['losses'], ops['pred'], ops['lr']], feed_dict=feed_dict)
        test_writer.add_summary(summary, step)
        pred_val_arg = np.argmax(pred_val, 1)
        batch_losses.append(loss_val)
        batch_idx += 1
        for i in range(0, bsize):
            l = batch_label[i]
            top3 = pred_val[i].argsort()[-3:][::-1]
            top3_correct += l in top3
            top3_class_correct[l] += l in top3
            confusion_matrix[pred_val_arg[i], l] += 1

    col_norm = np.maximum(np.nan_to_num(np.sum(confusion_matrix, axis=0)), 1)
    row_norm = np.maximum(np.nan_to_num(np.sum(confusion_matrix, axis=1)), 1)
    accuracy = np.sum(np.diag(confusion_matrix)) / np.sum(confusion_matrix)
    precision = np.round(np.diag(confusion_matrix) / row_norm, 2)
    recall = np.round(np.diag(confusion_matrix) / col_norm, 2)
    top3_accuracy = top3_correct / np.sum(confusion_matrix)
    top3_avg_class_acc = np.mean(top3_class_correct / col_norm)
    class_acc = np.diag(confusion_matrix) / col_norm
    avg_class_acc = np.mean(np.nan_to_num(class_acc))

    log_string(f'eval mean loss: {np.mean(batch_losses)}')
    log_string(f'eval accuracy: {accuracy}')
    log_string(f'eval avg class acc {avg_class_acc}')
    log_string(f'eval top3 acc {top3_accuracy}')
    log_string(f'eval top3 avg class acc {top3_avg_class_acc}')

    if WANDB:
        wandb.log(
            {'eval_mean_loss': np.mean(batch_losses),
            'eval_accuracy': accuracy,
            'eval_avg_class_acc': avg_class_acc,
            'eval_top3_acc': top3_accuracy,
            'eval_top3_avg_class_acc': top3_avg_class_acc,
#             'eval_confusion_matrix': wandb.Image(plot_conf_matrix(confusion_matrix, True)),
            'eval_avg_recall': np.mean(recall),
            'eval_avg_precision': np.mean(precision),
            'eval_precision': wandb.Table(
                columns=TRAIN_DATASET.classes_names,
                data=[precision.astype(str).tolist()]),
            'eval_recall': wandb.Table(
                columns=TRAIN_DATASET.classes_names,
                data=[recall.astype(str).tolist()]),
            },
            step=step
        )

    EPOCH_CNT += 1
    plt.close()
    TEST_DATASET.reset()
    return accuracy


if __name__ == "__main__":
    log_string('pid: %s'%(str(os.getpid())))
    train()
    LOG_FOUT.close()
