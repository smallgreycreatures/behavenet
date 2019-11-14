import os
import time
import numpy as np
import random
import pickle
import torch
from test_tube import HyperOptArgumentParser

from behavenet.fitting.eval import export_latents_best
from behavenet.fitting.eval import export_train_plots
from behavenet.fitting.training import fit
from behavenet.fitting.utils import build_data_generator
from behavenet.fitting.utils import create_tt_experiment
from behavenet.fitting.utils import export_hparams
from behavenet.fitting.utils import get_best_model_version
from behavenet.fitting.utils import get_output_session_dir
from behavenet.fitting.utils import get_user_dir
from behavenet.fitting.utils import add_lab_defaults_to_parser
from behavenet.fitting.ae_model_architecture_generator import draw_archs
from behavenet.fitting.ae_model_architecture_generator import draw_handcrafted_archs
from behavenet.models import AE as AE


def main(hparams):

    if not isinstance(hparams, dict):
        hparams = vars(hparams)
    hparams.pop('trials', False)
    hparams.pop('generate_trials', False)
    hparams.pop('optimize_parallel', False)
    hparams.pop('optimize_parallel_cpu', False)
    hparams.pop('optimize_parallel_gpu', False)
    hparams.pop('optimize_trials_parallel_gpu', False)
    if hparams['model_type'] == 'conv':
        # blend outer hparams with architecture hparams
        hparams = {**hparams, **hparams['architecture_params']}
        # get index of architecture in list
        if hparams['search_type'] == 'initial':
            list_of_archs = pickle.load(open(hparams['arch_file_name'], 'rb'))
            hparams['list_index'] = list_of_archs.index(hparams['architecture_params'])
        elif hparams['search_type'] == 'latent_search':
            hparams['architecture_params']['n_ae_latents'] = hparams['n_ae_latents']
            hparams['architecture_params'].pop('learning_rate', None)
    print('\nexperiment parameters:')
    print(hparams)

    # Start at random times (so test tube creates separate folders)
    np.random.seed(random.randint(0, 1000))
    time.sleep(np.random.uniform(1))

    # create test-tube experiment
    hparams, sess_ids, exp = create_tt_experiment(hparams)
    if hparams is None:
        return

    # build data generator
    data_generator = build_data_generator(hparams, sess_ids)

    # ####################
    # ### CREATE MODEL ###
    # ####################

    print('constructing model...', end='')
    torch.manual_seed(hparams['rng_seed_model'])
    torch_rnd_seed = torch.get_rng_state()
    hparams['model_build_rnd_seed'] = torch_rnd_seed
    hparams['n_datasets'] = len(sess_ids)
    model = AE(hparams)
    model.to(hparams['device'])
    model.version = exp.version
    torch_rnd_seed = torch.get_rng_state()
    hparams['training_rnd_seed'] = torch_rnd_seed

    # save out hparams as csv and dict
    hparams['training_completed'] = False
    export_hparams(hparams, exp)
    print('done')

    # ####################
    # ### TRAIN MODEL ###
    # ####################

    fit(hparams, model, data_generator, exp, method='ae')

    # export training plots
    if hparams['export_train_plots']:
        print('creating training plots...', end='')
        version_dir = os.path.join(hparams['expt_dir'], 'version_%i' % hparams['version'])
        save_file = os.path.join(version_dir, 'loss_training')
        export_train_plots(hparams, 'train', save_file=save_file)
        save_file = os.path.join(version_dir, 'loss_validation')
        export_train_plots(hparams, 'val', save_file=save_file)
        print('done')

    # update hparams upon successful training
    hparams['training_completed'] = True
    export_hparams(hparams, exp)


def get_params(strategy):

    # parser = HyperOptArgumentParser(strategy)
    from argparse import ArgumentParser
    parser = ArgumentParser(strategy)

    # most important arguments
    parser.add_argument('--search_type', choices=['latent_search', 'test'], type=str)
    parser.add_argument('--lab_example', type=str)  # musall, steinmetz, datta
    parser.add_argument('--save_dir', default=get_user_dir('save'), type=str)
    parser.add_argument('--data_dir', default=get_user_dir('data'), type=str)
    parser.add_argument('--model_type', type=str, choices=['conv', 'linear'])
    parser.add_argument('--model_class', default='ae', choices=['ae', 'vae'], type=str)
    parser.add_argument('--sessions_csv', default='', type=str, help='specify multiple sessions')

    # arguments for computing resources (infer n_gpu_workers from visible gpus)
    parser.add_argument('--tt_n_gpu_trials', default=1000, type=int)
    parser.add_argument('--tt_n_cpu_trials', default=1000, type=int)
    parser.add_argument('--tt_n_cpu_workers', default=5, type=int)
    parser.add_argument('--mem_limit_gb', default=8.0, type=float)
    parser.add_argument('--gpus_viz', default='0', type=str, help="add multiple as '0;1;4' etc")

    # add data generator arguments
    parser.add_argument('--device', default='cuda', choices=['cpu', 'cuda'], type=str)
    parser.add_argument('--as_numpy', action='store_true', default=False)
    parser.add_argument('--batch_load', action='store_true', default=True)
    parser.add_argument('--rng_seed_data', default=0, type=int, help='control data splits')
    parser.add_argument('--train_frac', default=1.0, type=float)

    # add fitting arguments
    parser.add_argument('--val_check_interval', default=1)
    parser.add_argument('--l2_reg', default=0)

    parser.add_argument('--export_train_plots', action='store_true', default=False)

    # get lab-specific arguments
    namespace, extra = parser.parse_known_args()
    add_lab_defaults_to_parser(parser, namespace.lab_example)

    # get model-type specific arguments
    if namespace.model_type == 'conv':
        get_conv_params(namespace, parser)
    elif namespace.model_type == 'linear':
        get_linear_params(namespace, parser)
    else:
        raise ValueError('"%s" is an invalid model type')

    return parser.parse_args()


def get_linear_params(namespace, parser):

    if namespace.search_type == 'test':

        parser.add_argument('--n_ae_latents', help='number of latents', type=int)
        parser.add_argument('--learning_rate', default=1e-4, type=float)

        parser.add_argument('--max_n_epochs', default=1000, type=int)
        parser.add_argument('--min_n_epochs', default=500, type=int)
        parser.add_argument('--experiment_name', default='test', type=str)
        parser.add_argument('--export_latents', action='store_true', default=False)
        parser.add_argument('--export_latents_best', action='store_true', default=False)
        parser.add_argument('--enable_early_stop', action='store_true', default=True)
        parser.add_argument('--early_stop_history', default=10, type=int)

    elif namespace.search_type == 'latent_search':

        parser.opt_list('--n_ae_latents', options=[4, 8, 12, 16, 32, 64], help='number of latents', type=int, tunable=True) # warning: over 64, may need to change max_latents in architecture generator
        parser.opt_list('--learning_rate', options=[1e-4, 1e-3], type=float, tunable=True)

        parser.add_argument('--max_n_epochs', default=1000, type=int)
        parser.add_argument('--min_n_epochs', default=500, type=int)
        parser.add_argument('--experiment_name', default='best', type=str)
        parser.add_argument('--export_latents', action='store_true', default=True)
        parser.add_argument('--export_latents_best', action='store_true', default=False)
        parser.add_argument('--enable_early_stop', action='store_true', default=True)
        parser.add_argument('--early_stop_history', default=10, type=int)

    else:
        raise Exception


def get_conv_params(namespace, parser):

    # get experiment-specific arguments
    if namespace.search_type == 'test':

        parser.add_argument('--n_ae_latents', help='number of latents', type=int)

        parser.add_argument('--fit_sess_io_layers', action='store_true', default=False)
        parser.add_argument('--which_handcrafted_archs', default='0')
        parser.add_argument('--max_n_epochs', default=1000, type=int)
        parser.add_argument('--min_n_epochs', default=500, type=int)
        parser.add_argument('--experiment_name', default='test', type=str)
        parser.add_argument('--export_latents', action='store_true', default=False)
        parser.add_argument('--export_latents_best', action='store_true', default=False)
        parser.add_argument('--enable_early_stop', action='store_true', default=True)
        parser.add_argument('--early_stop_history', default=10, type=int)

    elif namespace.search_type == 'initial':

        parser.add_argument('--arch_file_name', type=str) # file name where storing list of architectures (.pkl file), if exists, assumes already contains handcrafted archs!
        parser.add_argument('--n_ae_latents', help='number of latents', type=int)

        parser.add_argument('--fit_sess_io_layers', action='store_true', default=False)
        parser.add_argument('--which_handcrafted_archs', default='0;1') # empty string if you don't want any
        parser.add_argument('--n_archs', default=50, help='number of architectures to randomly sample', type=int)
        parser.add_argument('--max_n_epochs', default=20, type=int)
        parser.add_argument('--min_n_epochs', default=0, type=int)
        parser.add_argument('--experiment_name', default='initial_grid_search', type=str) # test
        parser.add_argument('--export_latents', action='store_true', default=False)
        parser.add_argument('--export_latents_best', action='store_true', default=False)
        parser.add_argument('--enable_early_stop', action='store_true', default=False)
        parser.add_argument('--early_stop_history', default=None, type=int)

    elif namespace.search_type == 'top_n':

        parser.add_argument('--saved_initial_archs', default='initial_grid_search', type=str) # experiment name to look for initial architectures in
        parser.add_argument('--n_ae_latents', help='number of latents', type=int)

        parser.add_argument('--fit_sess_io_layers', action='store_true', default=False)
        parser.add_argument('--n_top_archs', default=5, help='number of top architectures to run', type=int)
        parser.add_argument('--max_n_epochs', default=1000, type=int)
        parser.add_argument('--min_n_epochs', default=500, type=int)
        parser.add_argument('--experiment_name', default='top_n_grid_search', type=str)
        parser.add_argument('--export_latents', action='store_true', default=False)
        parser.add_argument('--export_latents_best', action='store_true', default=False)
        parser.add_argument('--enable_early_stop', action='store_true', default=True)
        parser.add_argument('--early_stop_history', default=10, type=int)

    elif namespace.search_type == 'latent_search':

        parser.add_argument('--source_n_ae_latents', help='number of latents', type=int)

        parser.add_argument('--fit_sess_io_layers', action='store_true', default=False)
        parser.add_argument('--saved_top_n_archs', default='top_n_grid_search', type=str) # experiment name to look for top n architectures in
        parser.add_argument('--max_n_epochs', default=1000, type=int)
        parser.add_argument('--min_n_epochs', default=500, type=int)
        parser.add_argument('--experiment_name', default='best', type=str)
        parser.add_argument('--export_latents', action='store_true', default=True)
        parser.add_argument('--export_latents_best', action='store_true', default=False)
        parser.add_argument('--enable_early_stop', action='store_true', default=True)
        parser.add_argument('--early_stop_history', default=10, type=int)

    else:
        raise ValueError('"%s" is not a valid search type' % namespace.search_type)

    namespace, extra = parser.parse_known_args()

    # Load in file of architectures
    if namespace.search_type == 'test':

        which_handcrafted_archs = np.asarray(namespace.which_handcrafted_archs.split(';')).astype('int')
        list_of_archs = draw_handcrafted_archs(
            [namespace.n_input_channels, namespace.y_pixels, namespace.x_pixels],
            namespace.n_ae_latents,
            which_handcrafted_archs,
            check_memory=True,
            batch_size=namespace.approx_batch_size,
            mem_limit_gb=namespace.mem_limit_gb)
        # TODO: fix test tube #2
        #parser.opt_list('--architecture_params', options=list_of_archs, tunable=True)
        parser.add_argument('--architecture_params', default=list_of_archs[0])
        parser.add_argument('--learning_rate', default=1e-4, type=float)

    elif namespace.search_type == 'initial':

        if os.path.isfile(namespace.arch_file_name):
            print('Using presaved list of architectures (not appending handcrafted architectures)')
            list_of_archs = pickle.load(open(namespace.arch_file_name, 'rb'))
        else:
            print('Creating new list of architectures and saving')
            list_of_archs = draw_archs(
                batch_size=namespace.approx_batch_size,
                input_dim=[namespace.n_input_channels, namespace.y_pixels, namespace.x_pixels],
                n_ae_latents=namespace.n_ae_latents,
                n_archs=namespace.n_archs,
                check_memory=True,
                mem_limit_gb=namespace.mem_limit_gb)
            if namespace.which_handcrafted_archs:
                which_handcrafted_archs = np.asarray(namespace.which_handcrafted_archs.split(';')).astype('int')
                list_of_handcrafted_archs = draw_handcrafted_archs(
                    [namespace.n_input_channels, namespace.y_pixels, namespace.x_pixels],
                    namespace.n_ae_latents,
                    which_handcrafted_archs,
                    check_memory=True,
                    batch_size=namespace.approx_batch_size,
                    mem_limit_gb=namespace.mem_limit_gb)
                list_of_archs = list_of_archs + list_of_handcrafted_archs
            f = open(namespace.arch_file_name, "wb")
            pickle.dump(list_of_archs, f)
            f.close()
        parser.opt_list('--architecture_params', options=list_of_archs, tunable=True)
        parser.add_argument('--learning_rate', default=1e-4, type=float)

    elif namespace.search_type == 'top_n':
        # Get top n architectures in directory
        session_dir, _ = get_output_session_dir(vars(namespace))
        initial_archs_dir = os.path.join(
            session_dir, namespace.model_class, 'conv',
            str('%02i_latents' % namespace.n_ae_latents),
            namespace.saved_initial_archs)
        best_versions = get_best_model_version(
            initial_archs_dir, n_best=namespace.n_top_archs)
        print(best_versions)
        list_of_archs=[]
        for version in best_versions:
             filename = os.path.join(initial_archs_dir, version, 'meta_tags.pkl')
             temp = pickle.load(open(filename, 'rb'))
             temp['architecture_params']['source_architecture'] = filename
             list_of_archs.append(temp['architecture_params'])
        parser.opt_list('--learning_rate', default=1e-4, options=[1e-4, 1e-3], type=float, tunable=True)
        parser.opt_list('--architecture_params', options=list_of_archs, tunable=True)

    elif namespace.search_type == 'latent_search':
        # Get top 1 architectures in directory
        session_dir, _ = get_output_session_dir(vars(namespace))
        initial_archs_dir = os.path.join(
            session_dir, namespace.model_class, 'conv',
            str('%02i_latents' % namespace.n_ae_latents),
            namespace.saved_initial_archs)
        best_version = get_best_model_version(initial_archs_dir, n_best=1)[0]
        filename = os.path.join(initial_archs_dir, best_version, 'meta_tags.pkl')
        arch = pickle.load(open(filename, 'rb'))
        arch['architecture_params']['source_architecture'] = filename
        arch['architecture_params'].pop('n_ae_latents', None)
        arch['architecture_params']['learning_rate'] = arch['learning_rate']
        # parser.add_argument('--learning_rate', default=arch['learning_rate'])
        parser.opt_list('--architecture_params', options=[arch['architecture_params']], type=float, tunable=True)  # have to pass in as a list since add_argument doesn't take dict
        parser.opt_list('--n_ae_latents', options=[4, 8, 12, 16, 24, 32, 64], help='number of latents', type=int, tunable=True)  # warning: over 64, may need to change max_latents in architecture generator


if __name__ == '__main__':

    hyperparams = get_params('grid_search')

    t = time.time()
    # TODO: fix test tube #1
    # if hyperparams.device == 'cuda' or hyperparams.device == 'gpu':
    #     if hyperparams.device == 'gpu':
    #         hyperparams.device = 'cuda'
    #     gpu_ids = hyperparams.gpus_viz.split(';')
    #     hyperparams.optimize_parallel_gpu(
    #         main,
    #         gpu_ids=gpu_ids,
    #         max_nb_trials=hyperparams.tt_n_gpu_trials,
    #         nb_workers=len(gpu_ids))
    # elif hyperparams.device == 'cpu':
    #     hyperparams.optimize_parallel_cpu(
    #         main,
    #         nb_trials=hyperparams.tt_n_cpu_trials,
    #         nb_workers=hyperparams.tt_n_cpu_workers)
    main(hyperparams)
    print('Total fit time: {} sec'.format(time.time() - t))
    if hyperparams.export_latents_best:
        print('Exporting latents from current best model in experiment')
        export_latents_best(vars(hyperparams))
