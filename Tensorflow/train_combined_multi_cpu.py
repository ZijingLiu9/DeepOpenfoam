import tensorflow as tf
import logging
from multiprocessing import Pool, Manager, Value
import argparse
import json
import os
from utils.custom_objects import chamfer_distance
from itertools import product
import tqdm
import numpy as np
import pandas as pd
from sklearn.model_selection import KFold, train_test_split
from sklearn.preprocessing import MinMaxScaler
from collections import OrderedDict, namedtuple
import tensorflow as tf
from utils.concatenate_sides import concatenate_sides
from utils.PointNetAE import create_pointnet_ae, OrthogonalRegularizer, Sampling
from utils.custom_objects import r_squared
logging.basicConfig(level=logging.INFO)
logging.info(tf.__version__)
print("Num GPUs Available: ", len(tf.config.list_physical_devices('GPU')))

try:
    for gpu in tf.config.experimental.list_physical_devices("GPU"): tf.config.experimental.set_virtual_device_configuration(
            gpu,
            [tf.config.experimental.VirtualDeviceConfiguration(memory_limit=40000)])
except Exception as e:
    logging.info(e)


def scale_y_points(x):
    # @TODO: how to denormalize data ?
    x_norm = x.copy()
    x_y = x_norm[:, :, 1].reshape(-1, 1)
    min_v_y = min(x_y) + 0.2 * min(x_y)  # @TODO: beky => check this
    max_v_y = max(x_y) + 0.2 * max(x_y)

    x_scaled_y = (x_y - min_v_y / (max_v_y - min_v_y))

    x_norm[:, :, 1] = x_scaled_y.reshape(x[:, :, 1].shape)
    return x_norm, min_v_y, max_v_y


def load_data(path: str = 'dataset'):
    pressure_side = np.load(f'{path}/pressure_side.npy')
    suction_side = np.load(f'{path}/suction_side.npy')
    data = concatenate_sides(suction_side, pressure_side)
    # escludo il campo di pressione dai dati
    data[:, :, [3, 4]] = data[:, :, [4, 3]]
    data = data[:, :, :4]
    print('Data shape: ', data.shape)

    global_variables_ = pd.read_csv(f'{path}/coefficients_clean.csv')
    global_variables_ = global_variables_.iloc[:, -2:].to_numpy()
    scaler_globals_ = MinMaxScaler()

    normed_global_variables_ = scaler_globals_.fit_transform(global_variables_)

    normed_geometries_, min_value_y, max_value_y = scale_y_points(data)

    return normed_geometries_, normed_global_variables_, scaler_globals_, min_value_y, max_value_y


class RunBuilder:
    def __init__(self):
        pass

    def __getstate__(self):
        return self.__dict__

    def __setstate__(self, d):
        self.__dict__ = d

    @staticmethod
    def get_runs(params, Run):
        runs = []
        for v in product(*params.values()):
            runs.append(Run(*v))

        return runs


def handle_results_path():
    if args.clear:
        if os.path.exists(args.results_path):
            os.system(f'rm -r {args.results_path}')
            os.system(f'rm -r {args.log_path}')
            os.mkdir(args.results_path)
            os.mkdir(args.log_path)
        else:
            os.mkdir(args.results_path)
            os.mkdir(args.log_path)
    else:
        if os.path.exists(args.results_path):
            raise ValueError(f'{args.results_path} already exists')
        else:
            os.mkdir(args.results_path)
            os.mkdir(args.log_path)


def single_run(run):

        run, run_count, run_data, args, train_data,\
            train_labels, val_data, val_labels = run

        run_count.value += 1
        print('\n--- New run detected ---')

        for key in run._asdict():
            print(f'{key}: {run._asdict()[key]}')

        run_path = os.path.join(args.results_path, str(run_count.value))

        print(f'\n Creating results path: {run_path}')
        os.mkdir(run_path)

        log_dir = os.path.join(args.log_path, str(run_count.value))
        os.mkdir(log_dir)
        tensorboard_callback = tf.keras.callbacks.TensorBoard(log_dir=log_dir, histogram_freq=1)
        checkpoints_callback = tf.keras.callbacks.ModelCheckpoint(
            run_path + '/checkpoint', monitor='val_loss', verbose=2, save_best_only=True,
            save_weights_only=False, mode='auto', period=200,  # @TODO
            options=None,
        )

        model = create_pointnet_ae(run,
                                   grid_size=4,
                                   n_geometry_points=400,
                                   n_global_variables=2, )

        model.summary()

        optimizer = tf.keras.optimizers.get(run.optimizer)
        optimizer.learning_rate = run.lr

        model.compile(
            optimizer=optimizer,
            loss=[chamfer_distance, tf.keras.metrics.mean_squared_error],
            metrics=dict(reg_gv=[r_squared]),
        )

        history = model.fit(train_data,
                            [train_data, train_labels],
                            batch_size=run.batch_size,
                            epochs=run.epochs,
                            # verbose=2,
                            shuffle=True,
                            validation_data=(val_data,
                                             [val_data, val_labels]),
                            validation_batch_size=run.batch_size,
                            callbacks=[tensorboard_callback, checkpoints_callback]
                            )

        model.save(os.path.join(run_path, 'model'))

        model = tf.keras.models.load_model(os.path.join(run_path, 'checkpoint'),
                                           custom_objects={'r_squared': r_squared,
                                                           'chamfer_distance': chamfer_distance,
                                                           'OrthogonalRegularizer': OrthogonalRegularizer,
                                                           'Sampling': Sampling})

        train_scores = model.evaluate(train_data, [train_data, train_labels])
        val_scores = model.evaluate(val_data, [val_data, val_labels])

        results = OrderedDict()
        results["run"] = run_count.value
        results["train_loss"], results["train_loss_ae"], results["train_loss_reg"] = train_scores[0], train_scores[1], \
                                                                                     train_scores[2]
        results["val_loss"], results["val_loss_ae"], results["val_loss_reg"] = val_scores[0], val_scores[1], \
                                                                               val_scores[2]
        results["train_r2"], results["val_r2"] = train_scores[3], val_scores[3]
        results["run_path"] = run_path

        for k, v in run._asdict().items(): results[k] = v
        run_data.value.append(results)
        df = pd.DataFrame.from_dict(run_data.value, orient='columns')
        df.to_excel(os.path.join(args.results_path, 'results.xlsx'))

        with open(os.path.join(run_path, 'log.json'), 'w', encoding='utf-8') as f:
            json.dump([results], f, ensure_ascii=False, indent=4)


params = OrderedDict(
        lr=[.01, .001, .0001],
        batch_size=[64, 256],
        epochs=[6, ],
        optimizer=['Adam'],
        type_decoder=['dense', 'cnn'],
        architectural_parameters=[[False, 0], [True, 1], [True, 5], [True, 15]],
        # [is_variational, beta],
        encoding_size=[10, 20, 50, 100],
        ort_reg_bools=[
            # [False, False],
            # [True, False],
            [True, True]
        ],  # [feature_transform, orto_reg]
        reg_drop_out_value=[0., 0.1, 0.3]
    )
Run = namedtuple('Run', params.keys())

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Training, validation and testing of AE and 3D regressor')

    # Required positional argument
    parser.add_argument('--data_path', type=str,
                        default='dataset',
                        help='dataset path')

    parser.add_argument('--results_path', type=str,
                        default='results',
                        help='results path')
    parser.add_argument('--log_path', type=str,
                        default='logs',
                        help='log path')
    parser.add_argument('--clear', type=bool,
                        default=True,  # @TODO: change
                        help='delete results path if exists')

    args = parser.parse_args()

    normed_geometries, normed_global_variables, scaler_globals, min_y, max_y = load_data(args.data_path)

    train_data, test_data, train_labels, test_labels = train_test_split(
        normed_geometries, normed_global_variables, test_size=0.1, shuffle=True)

    train_data, val_data, train_labels, val_labels = train_test_split(
        train_data, train_labels, test_size=0.1, shuffle=True)

    handle_results_path()

    manager = Manager()
    run_count = manager.Value('run_count', 0)
    run_data = manager.Value('run_data', [])

    print('train data', train_data.shape)
    print('validation data', val_data.shape)
    print('test data', test_data.shape)

    pool = Pool(processes=5)

    pool.map(single_run, [(n, run_count, run_data, args,
                           train_data, train_labels,
                           val_data, val_labels) for n in tqdm.tqdm(RunBuilder.get_runs(params, Run))])

    # for run in tqdm.tqdm(RunBuilder.get_runs(params)):

    # print('\n--- Final Df ---\n')
    # print(df)

    # df.sort_values('val_r2', axis=1, inplace=True)
    # best_model = tf.keras.models.load(os.path.join(df.run_path[0], 'model'))
    # best_model.evaluate(test_data, [test_data, test_labels])
    # best_model.save(os.path.join(args.results_path, 'best_model'))
