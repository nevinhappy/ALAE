# Copyright 2019 Stanislav Pidhorskyi
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#  http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from __future__ import print_function
import torch.utils.data
from scipy import misc
from torch import optim
from torchvision.utils import save_image
from net import *
import numpy as np
import pickle
import time
import random
import os
from dlutils import batch_provider
from dlutils.pytorch.cuda_helper import *
from dlutils.pytorch import count_parameters

im_size = 32


def save_model(x, name):
    if isinstance(x, nn.DataParallel):
        torch.save(x.module.state_dict(), name)
    else:
        torch.save(x.state_dict(), name)


def loss_function(recon_x, x):#, mu, logvar):
    BCE = torch.mean((recon_x - x)**2)

    # see Appendix B from VAE paper:
    # Kingma and Welling. Auto-Encoding Variational Bayes. ICLR, 2014
    # https://arxiv.org/abs/1312.6114
    # 0.5 * sum(1 + log(sigma^2) - mu^2 - sigma^2)
    #KLD = -0.5 * torch.mean(torch.mean(1 + logvar - mu.pow(2) - logvar.exp(), 1))
    return BCE#, KLD * 0.1


def process_batch(batch):
    data = [x[1] for x in batch]
    x = np.asarray(data, dtype=np.float32)
    x = torch.tensor(x, requires_grad=True).cuda() / 127.5 - 1.
    return x.view(-1, 1, x.shape[-2], x.shape[-1])

lod_2_batch = [256, 128, 128, 128, 128]


def D_logistic_simplegp(d_result_fake, d_result_real, reals, r1_gamma=10.0):
    loss = (F.softplus(d_result_fake) + F.softplus(-d_result_real)).mean()

    if r1_gamma != 0.0:
        real_loss = d_result_real.sum()
        real_grads = torch.autograd.grad(real_loss, reals, create_graph=True, retain_graph=True)[0]
        r1_penalty = torch.sum(real_grads.pow(2.0), dim=[1,2,3])
        loss = loss + r1_penalty.mean() * (r1_gamma * 0.5)
    return loss

    
def G_logistic_nonsaturating(d_result_fake):
    return F.softplus(-d_result_fake).mean()

    
def main(parallel=False):
    z_size = 512
    layer_count = 4
    epochs_per_lod = 4
    vae = VAE(zsize=z_size, layer_count=layer_count, maxf=128, channels=1)
    vae.cuda()
    vae.train()
    #vae.weight_init(mean=0, std=0.02)

    discriminator = Discriminator(zsize=z_size, layer_count=layer_count, maxf=128, channels=1)
    discriminator.cuda()
    discriminator.train()
    #discriminator.weight_init(mean=0, std=0.02)

    mapping = Mapping(num_layers=2 * layer_count)
    mapping.cuda()
    mapping.train()
    #mapping.weight_init(mean=0, std=0.02)

    bce_loss = nn.BCELoss()

    #vae.load_state_dict(torch.load("VAEmodel.pkl"))

    print("Trainable parameters autoencoder:")
    count_parameters(vae)

    print("Trainable parameters mapping:")
    count_parameters(mapping)

    print("Trainable parameters discriminator:")
    count_parameters(discriminator)

    if parallel:
        vae = nn.DataParallel(vae)
        discriminator = nn.DataParallel(discriminator)
        vae.layer_to_resolution = vae.module.layer_to_resolution

    lr = 0.001
    lr2 = 0.001

    vae_optimizer = optim.Adam([
        {'params': vae.parameters()},
        {'params': mapping.parameters(), 'lr': lr * 0.01}
    ], lr=lr, betas=(0.0, 0.99), weight_decay=0)

    discriminator_optimizer = optim.Adam(discriminator.parameters(), lr=lr2, betas=(0.0, 0.99), weight_decay=0)
 
    train_epoch = 18

    #sample1 = torch.randn(128, z_size).view(-1, z_size, 1, 1)
    sample = torch.randn(256, 64).view(-1, 64)

    lod = -1
    in_transition = False

    #for epoch in range(train_epoch):
    for epoch in range(train_epoch):
        vae.train()
        discriminator.train()

        new_lod = min(layer_count - 1, epoch // epochs_per_lod)
        #new_lod = max(new_lod, 2)
        if new_lod != lod:
            lod = new_lod
            print("#" * 80, "\n# Switching LOD to %d" % lod, "\n" + "#" * 80)
            print("Start transition")
            in_transition = True

            with open('data_fold_0_lod_%d.pkl' % (lod), 'rb') as pkl:
                data_train = pickle.load(pkl)
                random.shuffle(data_train)
                data_train=data_train
                
            print("Train set size:", len(data_train))
    
        new_in_transition = (epoch % epochs_per_lod) < (epochs_per_lod // 2) and lod > 0 and epoch // epochs_per_lod == lod
        if new_in_transition != in_transition:
            in_transition = new_in_transition
            print("#" * 80, "\n# Transition ended", "\n" + "#" * 80)


        random.shuffle(data_train)

        batches = batch_provider(data_train, lod_2_batch[lod], process_batch, report_progress=True)

        rec_loss = []
        kl_loss = []
        d_loss = []
        g_loss = []

        epoch_start_time = time.time()
        #
        # if (epoch + 1) == 40:
        #     vae_optimizer.param_groups[0]['lr'] = lr / 4
        #     discriminator_optimizer.param_groups[0]['lr'] = lr2 / 4
        #     print("learning rate change!")
        # if (epoch + 1) == 50:
        #     vae_optimizer.param_groups[0]['lr'] = lr / 4 / 4
        #     discriminator_optimizer.param_groups[0]['lr'] = lr2 / 4 / 4
        #     print("learning rate change!")

        i = 0
        for x_orig in batches:
            if x_orig.shape[0] != lod_2_batch[lod]:
                continue
            vae.train()
            discriminator.train()
            vae.zero_grad()
            discriminator.zero_grad()

            blend_factor = float((epoch % epochs_per_lod) * len(data_train) + i) / float(epochs_per_lod // 2 * len(data_train))
            if not in_transition:
                blend_factor = 1
            #else:
            #    print(blend_factor)

            #rec, mu, logvar = vae(x)

            needed_resolution = vae.layer_to_resolution[lod]
            #x = resize2d(x_orig, needed_resolution)
            x = x_orig

            if in_transition:
                needed_resolution_prev = vae.layer_to_resolution[lod - 1]
                x_prev = F.interpolate(x_orig, needed_resolution_prev)
                x_prev_2x = F.interpolate(x_prev, needed_resolution)
                x = x * blend_factor + x_prev_2x * (1.0 - blend_factor)

            #rec, rec_n = vae(x, x_prev, lod, blend_factor)
            z = torch.randn(lod_2_batch[lod], 64).view(-1, 64)
            w = mapping(z)

            rec = vae.forward(w, lod, blend_factor)

            d_result_real = discriminator(x, lod, blend_factor).squeeze()
            d_result_fake = discriminator(rec.detach(), lod, blend_factor).squeeze()
                
            loss_d = D_logistic_simplegp(d_result_fake, d_result_real, x)
            discriminator.zero_grad()
            loss_d.backward()
            d_loss += [loss_d.item()]

            discriminator_optimizer.step()
            
            ############################################################
            vae.zero_grad()

            z = torch.randn(lod_2_batch[lod], 64).view(-1, 64)
            w = mapping(z)

            rec = vae.forward(w, lod, blend_factor)

            #loss_re = loss_function(rec, x)
            #rec_loss += [loss_re.item()]
            
            d_result_fake = discriminator(rec, lod, blend_factor).squeeze()
            loss_g = G_logistic_nonsaturating(d_result_fake)
            loss_g.backward()
            g_loss += [loss_g.item()]

            vae_optimizer.step()
            
            #kl_loss += loss_kl.item()

            #############################################
            
            i += lod_2_batch[lod]

        os.makedirs('results_rec', exist_ok=True)
        os.makedirs('results_gen', exist_ok=True)
        
        epoch_end_time = time.time()
        per_epoch_ptime = epoch_end_time - epoch_start_time
        
        def avg(lst): 
            if len(lst) == 0:
                return 0
            return sum(lst) / len(lst) 
            

        rec_loss = avg(rec_loss)
        kl_loss = avg(kl_loss)
        g_loss = avg(g_loss)
        d_loss = avg(d_loss)
        print('\n[%d/%d] - ptime: %.2f, rec loss: %.9f, g loss: %.9f, d loss: %.9f' % (
            (epoch + 1), train_epoch, per_epoch_ptime, rec_loss, g_loss, d_loss))
        g_loss = []
        d_loss = []
        rec_loss = []
        kl_loss = []
        with torch.no_grad():
            vae.eval()
            w = list(mapping(sample))
            x_rec = vae(w, lod, blend_factor)
            resultsample = torch.cat([x, x_rec]) * 0.5 + 0.5
            resultsample = resultsample.cpu()
            save_image(resultsample.view(-1, 1, needed_resolution, needed_resolution),
                       'results_rec/sample_' + str(epoch) + "_" + str(i // lod_2_batch[lod]) + '.png', nrow=16)
            #x_rec = vae.decode(sample1)
            #resultsample = x_rec * 0.5 + 0.5
            #resultsample = resultsample.cpu()
            #save_image(resultsample.view(-1, 3, im_size, im_size),
            #           'results_gen/sample_' + str(epoch) + "_" + str(i) + '.png')

        del batches
        save_model(vae, "VAEmodel_tmp.pkl")
        save_model(mapping, "mapping_tmp.pkl")
        save_model(discriminator, "discriminator_tmp.pkl")
    print("Training finish!... save training results")
    save_model(vae, "VAEmodel.pkl")
    save_model(mapping, "mapping.pkl")
    save_model(discriminator, "discriminator.pkl")

if __name__ == '__main__':
    main(True)
