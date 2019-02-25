from __future__ import division
import os
import time
from glob import glob
import tensorflow as tf
import numpy as np
from collections import namedtuple

from supp_network import Vgg19_simple_api
import tensorlayer

from module import *
from utils import *

class sggan(object):
    def __init__(self, sess, args):
        self.sess = sess
        self.batch_size = args.batch_size
        self.image_width = args.img_width
        self.image_height = args.img_height
        self.input_c_dim = args.input_nc
        self.output_c_dim = args.output_nc
        self.L1_lambda = args.L1_lambda
        self.Lg_lambda = args.Lg_lambda
        self.dataset_dir = args.dataset_dir
        self.segment_class = args.segment_class

        self.discriminator = discriminator
        if args.use_resnet:
            self.generator = generator_resnet
        else:
            self.generator = generator_unet
        if args.use_lsgan:
            self.criterionGAN = mae_criterion
        else:
            self.criterionGAN = sce_criterion

        OPTIONS = namedtuple('OPTIONS', 'batch_size image_height image_width \
                              gf_dim df_dim output_c_dim is_training segment_class')
        self.options = OPTIONS._make((args.batch_size, args.img_height, args.img_width,
                                      args.ngf, args.ndf, args.output_nc,
                                      args.phase == 'train', args.segment_class))

        self._build_model()
        self.saver = tf.train.Saver()
        self.pool = ImagePool(args.max_size)


    def _build_model(self):
        self.real_data = tf.placeholder(tf.float32,
                                        [None, self.image_height, self.image_width,
                                         self.input_c_dim + self.output_c_dim],
                                        name='real_A_and_B_images')
        self.seg_data = tf.placeholder(tf.float32,
                                        [None, self.image_height, self.image_width,
                                         self.input_c_dim + self.output_c_dim],
                                        name='seg_A_and_B_images')
        self.mask_A = tf.placeholder(tf.float32, [None, self.image_height/8, self.image_width/8, self.segment_class], name='mask_A')
        self.mask_B = tf.placeholder(tf.float32, [None, self.image_height/8, self.image_width/8, self.segment_class], name='mask_B')


        self.real_A = self.real_data[:, :, :, :self.input_c_dim]
        self.real_B = self.real_data[:, :, :, self.input_c_dim:self.input_c_dim + self.output_c_dim]
        self.seg_A = self.seg_data[:, :, :, :self.input_c_dim]
        self.seg_B = self.seg_data[:, :, :, self.input_c_dim:self.input_c_dim + self.output_c_dim]


        # gradient kernel for seg
        # assume input_c_dim == output_c_dim
        self.kernels = []
        self.kernels.append( tf_kernel_prep_3d(np.array([[0,0,0],[-1,0,1],[0,0,0]]), self.input_c_dim) )
        self.kernels.append( tf_kernel_prep_3d(np.array([[0,-1,0],[0,0,0],[0,1,0]]), self.input_c_dim) )
        self.kernel = tf.constant(np.stack(self.kernels, axis=-1), name="DerivKernel_seg", dtype=np.float32)

        self.seg_A = tf.pad(self.seg_A, [[0, 0], [1, 1], [1, 1], [0, 0]], "REFLECT")
        self.seg_B = tf.pad(self.seg_B, [[0, 0], [1, 1], [1, 1], [0, 0]], "REFLECT")
        self.conved_seg_A = tf.abs(tf.nn.depthwise_conv2d(self.seg_A, self.kernel, [1, 1, 1, 1], padding="VALID", name="conved_seg_A"))
        self.conved_seg_B = tf.abs(tf.nn.depthwise_conv2d(self.seg_B, self.kernel, [1, 1, 1, 1], padding="VALID", name="conved_seg_B"))
        # change weighted_seg from (1.0, 0.0) to (0.9, 0.1) for soft gradient-sensitive loss
        self.weighted_seg_A = tf.abs(tf.sign(tf.reduce_sum(self.conved_seg_A, axis=-1, keep_dims=True)))
        self.weighted_seg_B = tf.abs(tf.sign(tf.reduce_sum(self.conved_seg_B, axis=-1, keep_dims=True)))
        #self.weighted_seg_A = 0.9 * tf.abs(tf.sign(tf.reduce_sum(self.conved_seg_A, axis=-1, keep_dims=True))) + 0.1
        #self.weighted_seg_B = 0.9 * tf.abs(tf.sign(tf.reduce_sum(self.conved_seg_B, axis=-1, keep_dims=True))) + 0.1

        #self.weighted_seg_A = tf.sign(tf.reduce_sum(self.conved_seg_A, axis=-1, keep_dims=True))
        #self.weighted_seg_B = tf.sign(tf.reduce_sum(self.conved_seg_B, axis=-1, keep_dims=True))
        #self.weighted_seg_A = 0.9 * tf.sign(tf.reduce_sum(self.conved_seg_A, axis=-1, keep_dims=True)) + 0.1
        #self.weighted_seg_B = 0.9 * tf.sign(tf.reduce_sum(self.conved_seg_B, axis=-1, keep_dims=True)) + 0.1


        self.fake_B = self.generator(self.real_A, self.options, False, name="generatorA2B")
        self.fake_A_ = self.generator(self.fake_B, self.options, False, name="generatorB2A")
        self.fake_A = self.generator(self.real_B, self.options, True, name="generatorB2A")
        self.fake_B_ = self.generator(self.fake_A, self.options, True, name="generatorA2B")

        ###============================= VGG LOSS===============================### 
        t_target_image_224 = tf.image.resize_images( self.real_A, size=[224, 224], method=0, align_corners=False)  # resize_target_image_for_vgg # http://tensorlayer.readthedocs.io/en/latest/_modules/tensorlayer/layers.html#UpSampling2dLayer
        t_predict_image_224 = tf.image.resize_images(self.fake_A_, size=[224, 224], method=0, align_corners=False)  # resize_generate_image_for_vgg

        net_vgg, vgg_target_emb = Vgg19_simple_api((t_target_image_224 + 1) / 2, reuse=False)
        _, vgg_predict_emb = Vgg19_simple_api((t_predict_image_224 + 1) / 2, reuse=True)        
        vgg_loss =1e+05* tensorlayer.cost.mean_squared_error(vgg_predict_emb.outputs, vgg_target_emb.outputs, is_mean=True)
        self.vgg_loss = vgg_loss

        t_target_image_224_2 = tf.image.resize_images( self.real_B, size=[224, 224], method=0, align_corners=False)  # resize_target_image_for_vgg # http://tensorlayer.readthedocs.io/en/latest/_modules/tensorlayer/layers.html#UpSampling2dLayer
        t_predict_image_224_2 = tf.image.resize_images(self.fake_B_, size=[224, 224], method=0, align_corners=False)  # resize_generate_image_for_vgg

        net_vgg_2, vgg_target_emb_2 = Vgg19_simple_api((t_target_image_224_2 + 1) / 2, reuse=True)
        _, vgg_predict_emb_2 = Vgg19_simple_api((t_predict_image_224_2 + 1) / 2, reuse=True)       
        vgg_loss_2 =1e+05* tensorlayer.cost.mean_squared_error(vgg_predict_emb_2.outputs, vgg_target_emb_2.outputs, is_mean=True)
        self.vgg_loss_2 = vgg_loss_2
        ###======================================================================###

        self.DB_fake = self.discriminator(self.fake_B, self.mask_A, self.options, reuse=False, name="discriminatorB")
        self.DA_fake = self.discriminator(self.fake_A, self.mask_B, self.options, reuse=False, name="discriminatorA")
        self.g_loss_a2b = self.criterionGAN(self.DB_fake, tf.ones_like(self.DB_fake)) \
            + self.L1_lambda * abs_criterion(self.real_A, self.fake_A_) \
            + self.L1_lambda * abs_criterion(self.real_B, self.fake_B_) \
            + self.Lg_lambda * gradloss_criterion(self.real_A, self.fake_B, self.weighted_seg_A) \
            + self.Lg_lambda * gradloss_criterion(self.real_B, self.fake_A, self.weighted_seg_B) 
            
        self.g_loss_b2a = self.criterionGAN(self.DA_fake, tf.ones_like(self.DA_fake)) \
            + self.L1_lambda * abs_criterion(self.real_A, self.fake_A_) \
            + self.L1_lambda * abs_criterion(self.real_B, self.fake_B_) \
            + self.Lg_lambda * gradloss_criterion(self.real_A, self.fake_B, self.weighted_seg_A) \
            + self.Lg_lambda * gradloss_criterion(self.real_B, self.fake_A, self.weighted_seg_B) 
            
        self.g_loss = self.criterionGAN(self.DA_fake, tf.ones_like(self.DA_fake)) \
            + self.criterionGAN(self.DB_fake, tf.ones_like(self.DB_fake)) \
            + self.L1_lambda * abs_criterion(self.real_A, self.fake_A_) \
            + self.L1_lambda * abs_criterion(self.real_B, self.fake_B_) \
            + self.Lg_lambda * gradloss_criterion(self.real_A, self.fake_B, self.weighted_seg_A) \
            + self.Lg_lambda * gradloss_criterion(self.real_B, self.fake_A, self.weighted_seg_B) \
            +vgg_loss_2 + vgg_loss

        #fake_A
        self.fake_A_sample = tf.placeholder(tf.float32,
                                            [None, self.image_height, self.image_width,
                                             self.input_c_dim], name='fake_A_sample')
        #fake_B
        self.fake_B_sample = tf.placeholder(tf.float32,
                                            [None, self.image_height, self.image_width,
                                             self.output_c_dim], name='fake_B_sample')
        self.mask_A_sample = tf.placeholder(tf.float32, [None, self.image_height/8, self.image_width/8, self.segment_class], name='mask_A_sample')
        self.mask_B_sample = tf.placeholder(tf.float32, [None, self.image_height/8, self.image_width/8, self.segment_class], name='mask_B_sample')


        self.DB_real = self.discriminator(self.real_B, self.mask_B, self.options, reuse=True, name="discriminatorB")
        self.DA_real = self.discriminator(self.real_A, self.mask_A, self.options, reuse=True, name="discriminatorA")
        self.DB_fake_sample = self.discriminator(self.fake_B_sample, self.mask_B_sample, self.options, reuse=True, name="discriminatorB")
        self.DA_fake_sample = self.discriminator(self.fake_A_sample, self.mask_A_sample, self.options, reuse=True, name="discriminatorA")

        self.db_loss_real = self.criterionGAN(self.DB_real, tf.ones_like(self.DB_real))
        self.db_loss_fake = self.criterionGAN(self.DB_fake_sample, tf.zeros_like(self.DB_fake_sample))
        self.db_loss = (self.db_loss_real + self.db_loss_fake) / 2
        self.da_loss_real = self.criterionGAN(self.DA_real, tf.ones_like(self.DA_real))
        self.da_loss_fake = self.criterionGAN(self.DA_fake_sample, tf.zeros_like(self.DA_fake_sample))
        self.da_loss = (self.da_loss_real + self.da_loss_fake) / 2
        self.d_loss = self.da_loss + self.db_loss

        self.g_loss_a2b_sum = tf.summary.scalar("g_loss_a2b", self.g_loss_a2b)
        self.g_loss_b2a_sum = tf.summary.scalar("g_loss_b2a", self.g_loss_b2a)
        self.g_loss_sum = tf.summary.scalar("g_loss", self.g_loss)
        self.g_sum = tf.summary.merge([self.g_loss_a2b_sum, self.g_loss_b2a_sum, self.g_loss_sum])
        self.db_loss_sum = tf.summary.scalar("db_loss", self.db_loss)
        self.da_loss_sum = tf.summary.scalar("da_loss", self.da_loss)
        self.d_loss_sum = tf.summary.scalar("d_loss", self.d_loss)
        self.db_loss_real_sum = tf.summary.scalar("db_loss_real", self.db_loss_real)
        self.db_loss_fake_sum = tf.summary.scalar("db_loss_fake", self.db_loss_fake)
        self.da_loss_real_sum = tf.summary.scalar("da_loss_real", self.da_loss_real)
        self.da_loss_fake_sum = tf.summary.scalar("da_loss_fake", self.da_loss_fake)
        self.d_sum = tf.summary.merge(
            [self.da_loss_sum, self.da_loss_real_sum, self.da_loss_fake_sum,
             self.db_loss_sum, self.db_loss_real_sum, self.db_loss_fake_sum,
             self.d_loss_sum]
        )

        self.test_A = tf.placeholder(tf.float32,
                                     [None, self.image_height, self.image_width,
                                      self.input_c_dim], name='test_A')
        self.test_B = tf.placeholder(tf.float32,
                                     [None, self.image_height, self.image_width,
                                      self.output_c_dim], name='test_B')
        self.testB = self.generator(self.test_A, self.options, True, name="generatorA2B")
        self.testA = self.generator(self.test_B, self.options, True, name="generatorB2A")

        t_vars = tf.trainable_variables()
        self.d_vars = [var for var in t_vars if 'discriminator' in var.name]
        self.g_vars = [var for var in t_vars if 'generator' in var.name]



        ###============================= LOAD VGG ===============================###
        vgg19_npy_path = "vgg19.npy"
        if not os.path.isfile(vgg19_npy_path):
                print("Please download vgg19.npz from : https://github.com/machrisaa/tensorflow-vgg")
                exit()
        npz = np.load(vgg19_npy_path, encoding='latin1').item()
        
        params = []
        for val in sorted(npz.items()):
                W = np.asarray(val[1][0])
                b = np.asarray(val[1][1])
                print("  Loading %s: %s, %s" % (val[0], W.shape, b.shape))
                params.extend([W, b])
        tensorlayer.files.assign_params(self.sess, params, net_vgg)
            # net_vgg.print_params(False)
            # net_vgg.print_layers()
        ###=====================================================================###


        for var in t_vars: print(var.name)

    def train(self, args):
        """Train SG-GAN"""

        self.lr = tf.placeholder(tf.float32, None, name='learning_rate')
        self.d_optim = tf.train.AdamOptimizer(self.lr, beta1=args.beta1) \
            .minimize(self.d_loss, var_list=self.d_vars)
        self.g_optim = tf.train.AdamOptimizer(self.lr, beta1=args.beta1) \
            .minimize(self.g_loss, var_list=self.g_vars)

        init_op = tf.global_variables_initializer()
        self.sess.run(init_op)
        self.writer = tf.summary.FileWriter("./logs", self.sess.graph)

        counter = 1
        start_time = time.time()

        if args.continue_train and self.load(args.checkpoint_dir):
            print(" [*] Load SUCCESS")
        else:
            print(" [!] Load failed...")
        for epoch in range(args.epoch):
            dataA = glob('./datasets/{}/*.*'.format(self.dataset_dir + '/trainA'))
            dataB = glob('./datasets/{}/*.*'.format(self.dataset_dir + '/trainB'))
            np.random.shuffle(dataA)
            np.random.shuffle(dataB)
            batch_idxs = min(min(len(dataA), len(dataB)), args.train_size) // self.batch_size
            lr = args.lr if epoch < args.epoch_step else args.lr*(args.epoch-epoch)/(args.epoch-args.epoch_step)

            for idx in range(0, batch_idxs):
                batch_files = list(zip(dataA[idx * self.batch_size:(idx + 1) * self.batch_size],
                                       dataB[idx * self.batch_size:(idx + 1) * self.batch_size]))
                batch_images = []
                batch_segs = []
                batch_seg_mask_A = []
                batch_seg_mask_B = []
                for batch_file in batch_files:
                    tmp_image, tmp_seg, tmp_seg_mask_A, tmp_seg_mask_B = load_train_data(batch_file, args.img_width, args.img_height, num_seg_masks=self.segment_class)
                    batch_images.append(tmp_image)
                    batch_segs.append(tmp_seg)
                    batch_seg_mask_A.append(tmp_seg_mask_A)
                    batch_seg_mask_B.append(tmp_seg_mask_B)

                batch_images = np.array(batch_images).astype(np.float32)
                batch_segs = np.array(batch_segs).astype(np.float32)
                batch_seg_mask_A = np.array(batch_seg_mask_A).astype(np.float32)
                batch_seg_mask_B = np.array(batch_seg_mask_B).astype(np.float32)
                
                # Update G network and record fake outputs
                fake_A, fake_B, fake_A_mask, fake_B_mask, _, summary_str,aaa,vgg_loss1,vgg_loss1_2 = self.sess.run(
                    [self.fake_A, self.fake_B, self.mask_B, self.mask_A, self.g_optim, self.g_sum, self.g_loss,self.vgg_loss,self.vgg_loss_2 ],
                    feed_dict={self.real_data: batch_images, self.lr: lr, self.seg_data: batch_segs,
                    self.mask_A: batch_seg_mask_A, self.mask_B: batch_seg_mask_B})
                print("G_loss: "+str(aaa) ) 
                print("vgg_loss: "+str(vgg_loss1))
                print("vgg_loss_22: "+str(vgg_loss1_2))
                self.writer.add_summary(summary_str, counter)
                [fake_A, fake_B, fake_A_mask, fake_B_mask] = self.pool([fake_A, fake_B, fake_A_mask, fake_B_mask])
                # Update D network
                _, summary_str,aaa = self.sess.run(
                    [self.d_optim, self.d_sum, self.d_loss],
                    feed_dict={self.real_data: batch_images,
                               self.fake_A_sample: fake_A,
                               self.fake_B_sample: fake_B,
                               self.mask_A_sample: fake_A_mask,
                               self.mask_B_sample: fake_B_mask,
                               self.mask_A: batch_seg_mask_A,
                               self.mask_B: batch_seg_mask_B,
                               self.lr: lr})
                print(aaa)    
                self.writer.add_summary(summary_str, counter)

                counter += 1
                print(("Epoch: [%2d] [%4d/%4d] time: %4.4f" % (
                    epoch, idx, batch_idxs, time.time() - start_time)))

                if np.mod(counter, args.print_freq) == 1:
                    self.sample_model(args.sample_dir, epoch, idx)

                if np.mod(counter, args.save_freq) == 2:
                    self.save(args.checkpoint_dir, counter)

    def save(self, checkpoint_dir, step):
        model_name = "sggan.model"
        model_dir = "%s" % self.dataset_dir
        checkpoint_dir = os.path.join(checkpoint_dir, model_dir)

        if not os.path.exists(checkpoint_dir):
            os.makedirs(checkpoint_dir)

        self.saver.save(self.sess,
                        os.path.join(checkpoint_dir, model_name),
                        global_step=step)

    def load(self, checkpoint_dir):
        print(" [*] Reading checkpoint...")

        model_dir = "%s" % self.dataset_dir
        checkpoint_dir = os.path.join(checkpoint_dir, model_dir)

        ckpt = tf.train.get_checkpoint_state(checkpoint_dir)
        if ckpt and ckpt.model_checkpoint_path:
            ckpt_name = os.path.basename(ckpt.model_checkpoint_path)
            self.saver.restore(self.sess, os.path.join(checkpoint_dir, ckpt_name))
            return True
        else:
            return False

    def sample_model(self, sample_dir, epoch, idx):
        dataA = glob('./datasets/{}/*.*'.format(self.dataset_dir + '/testA'))
        dataB = glob('./datasets/{}/*.*'.format(self.dataset_dir + '/testB'))
        np.random.shuffle(dataA)
        np.random.shuffle(dataB)
        batch_files = list(zip(dataA[:self.batch_size], dataB[:self.batch_size]))
        batch_images = []
        batch_segs = []
        for batch_file in batch_files:
            tmp_image, tmp_seg, tmp_seg_mask_A, tmp_seg_mask_B = load_train_data(batch_file, self.image_width, self.image_height, is_testing=True, num_seg_masks=self.segment_class)
            batch_images.append(tmp_image)
            batch_segs.append(tmp_seg)
               
        batch_images = np.array(batch_images).astype(np.float32)
        batch_segs = np.array(batch_segs).astype(np.float32)

        fake_A, fake_B = self.sess.run(
            [self.fake_A, self.fake_B],
            feed_dict={self.real_data: batch_images, self.seg_data: batch_segs}
        )
        save_images(fake_A, [self.batch_size, 1],
                    './{}/A_{:02d}_{:04d}_{}.jpg'.format(sample_dir, epoch, idx, batch_files[0][1].split("/")[-1].split(".")[0]))
        save_images(fake_B, [self.batch_size, 1],
                    './{}/B_{:02d}_{:04d}_{}.jpg'.format(sample_dir, epoch, idx, batch_files[0][0].split("/")[-1].split(".")[0]))

    def test(self, args):
        """Test SG-GAN"""
        init_op = tf.global_variables_initializer()
        self.sess.run(init_op)
        if args.which_direction == 'AtoB':
            sample_files = glob('./datasets/{}/*.*'.format(self.dataset_dir + '/testA'))
        elif args.which_direction == 'BtoA':
            sample_files = glob('./datasets/{}/*.*'.format(self.dataset_dir + '/testB'))
        else:
            raise Exception('--which_direction must be AtoB or BtoA')

        if self.load(args.checkpoint_dir):
            print(" [*] Load SUCCESS")
        else:
            print(" [!] Load failed...")

        # write html for visual comparison
        index_path = os.path.join(args.test_dir, '{0}_index.html'.format(args.which_direction))
        index = open(index_path, "w")
        index.write("<html><body><table><tr>")
        index.write("<th>name</th><th>input</th><th>output</th></tr>")

        out_var, in_var = (self.testB, self.test_A) if args.which_direction == 'AtoB' else (
            self.testA, self.test_B)

        for sample_file in sample_files:
            print('Processing image: ' + sample_file)
            sample_image = [load_test_data(sample_file, args.img_width, args.img_height)]
            sample_image = np.array(sample_image).astype(np.float32)
            image_path = os.path.join(args.test_dir, os.path.basename(sample_file))
            fake_img = self.sess.run(out_var, feed_dict={in_var: sample_image})
            real_image_copy = os.path.join(args.test_dir, "real_" + os.path.basename(sample_file))
            save_images(sample_image, [1, 1], real_image_copy)
            save_images(fake_img, [1, 1], image_path)
            index.write("<td>%s</td>" % os.path.basename(image_path))
            index.write("<td><img src='%s'></td>" % (real_image_copy if os.path.isabs(real_image_copy) else (
                '..' + os.path.sep + real_image_copy)))
            index.write("<td><img src='%s'></td>" % (image_path if os.path.isabs(image_path) else (
                '..' + os.path.sep + image_path)))
            index.write("</tr>")
        index.close()
