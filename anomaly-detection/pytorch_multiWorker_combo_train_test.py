# %%
import os
from glob import iglob

import lime
import lime.lime_tabular
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import scikitplot as skplt
import syft as sy
import torch
import torch.nn as nn
import torch.optim as optim
from absl import app
from absl import flags
from sklearn.metrics import recall_score, accuracy_score, precision_score, \
    confusion_matrix, classification_report
from sklearn.preprocessing import StandardScaler
from syft.federated.floptimizer import Optims
from tqdm import tqdm

# %%

flags.DEFINE_integer("Batch_size", 64, "The size of the batch from a round of training")
flags.DEFINE_integer("Epochs", 5, "The number of rounds of training")
flags.DEFINE_float("Learn_rate", 0.001, "The rate of learning by the optimizer")
flags.DEFINE_integer("Input_dim", 10, "the input dimension, used from getting the train data")
flags.DEFINE_string("Current_dir", os.path.dirname(os.path.abspath(__file__)), "the current directory")
FLAGS = flags.FLAGS
hook = sy.TorchHook(torch)
v_hook = sy.VirtualWorker(hook=hook, id="v_hook")
x_hook = sy.VirtualWorker(hook=hook, id="x_hook")
eval_hook = sy.VirtualWorker(hook=hook, id="eval")
tester_hook = sy.VirtualWorker(hook=hook, id="testing")
workers = ["v_hook", "x_hook", "eval", "testing"]
if torch.cuda.is_available():
    device0 = torch.device("cuda:0")
    device1 = torch.device("cuda:1")
    # device2 = torch.device("cuda:2")
    print("Running on the GPU")
else:
    device0 = torch.device("cpu")
    device1 = torch.device("cpu")
    # device2 = torch.device("cpu")
    print("Running on the CPU")


# %%
def load_mal_data():
    df_mirai = pd.concat((pd.read_csv(f) for f in iglob("../data/**/mirai_attacks/*.csv", recursive=True)),
                         ignore_index=True)
    df_gafgyt = pd.DataFrame()
    for f in iglob("../data/**/gafgyt_attacks/*.csv", recursive=True):
        #    if 'tcp.csv' in f or 'udp.csv' in f:
        #        continue
        df_gafgyt = df_gafgyt.append(pd.read_csv(f), ignore_index=True)
    return df_mirai.append(df_gafgyt)


# %%
def get_train_data(top_n_features=10):
    print("Loading combined training data...")
    df = pd.concat((
        pd.read_csv(f) for f in iglob('../data/**/benign_traffic.csv', recursive=True)),
        ignore_index=True)
    fisher = pd.read_csv('../fisher.csv')
    features = fisher.iloc[0:int(top_n_features)]['Feature'].values
    df = df[list(features)]
    return df, top_n_features, features


# %%
def create_scalar(x_opt, x_test, x_train):
    scalar = StandardScaler()
    scalar.fit(x_train.append(x_opt))
    x_train = scalar.transform(x_train)
    x_opt = scalar.transform(x_opt)
    x_test = scalar.transform(x_test)
    return x_train, x_opt, x_test, scalar


# %%
def train(net, x_train, x_opt, batch_size, epochs, learn_rate):
    optimizer = optim.SGD(net.parameters(), lr=learn_rate)
    loss_function = nn.MSELoss()
    batch_x, batch_y, data, outputs, loss = 0, 0, 0, 0, 0
    optims = Optims(workers, optim=optimizer)
    for epoch in range(epochs):
        for i in tqdm(range(0, len(x_train), batch_size)):
            batch_x = x_train[i:i + batch_size].to(device0)
            batch_y = x_opt[i:i + batch_size].to(device1)
            data_x = batch_x[::2]
            data_y = batch_x[1::2]
            target_x = batch_y[::2]
            target_y = batch_y[1::2]
            data_y = data_y.send(x_hook)
            data_x = data_x.send(v_hook)
            target_x = target_x.send(v_hook)
            target_y = target_y.send(x_hook)
            datasets = [(data_x, target_x), (data_y, target_y)]
            for data, target in datasets:
                net.send(data.location)
                opt = optims.get_optim(data.location.id)
                opt.zero_grad()
                outputs = net(data)
                loss = loss_function(outputs, data)
                loss.backward()
                opt.step()
                net.get()
            if torch.cuda.is_available():
                torch.cuda.synchronize()
        print(f"Epoch: {epoch}. Loss 1: {loss.get():.3f}")
    return np.mean(np.power(data.get().cpu().data.numpy() - outputs.get().cpu().data.numpy(), 2), axis=1)


# %%
def cal_threshold(mse, input_dim):
    print("mean is %.5f" % mse.mean())
    print("min is %.5f" % mse.min())
    print("max is %.5f" % mse.max())
    print("std is %.5f" % mse.std())
    tr = mse.mean() + mse.std()
    with open(f"threshold_multiworker/threshold_federated_{input_dim}_{FLAGS.Learn_rate}.txt", 'w') as t:
        t.write(str(tr))
    print(f"Calculated threshold is {tr}")
    return tr


# %%
def evaluation(net, x_test, tr):
    if torch.cuda.is_available():
        torch.cuda.synchronize()
    x_test = x_test.to(device1)
    x_test = x_test.send(eval_hook)
    net.eval()
    net.send(x_test.location)
    x_test_predictions = net(x_test)
    print("Calculating MSE on test set...")
    mse_test = np.mean(np.power(x_test.get().cpu().data.numpy() - x_test_predictions.get().cpu().data.numpy(), 2),
                       axis=1)
    over_tr = mse_test > tr
    false_positives = sum(over_tr)
    test_size = mse_test.shape[0]
    print(f"{false_positives} false positives on dataset without attacks with size {test_size}")


# %%
class Net(nn.Module):
    def __init__(self, input_dim=10):
        super().__init__()
        self.fc1 = nn.Linear(input_dim, int(0.75 * input_dim))
        self.fc2 = nn.Linear(int(0.75 * input_dim), int(0.5 * input_dim))
        self.fc3 = nn.Linear(int(0.5 * input_dim), int(0.33 * input_dim))
        self.fc4 = nn.Linear(int(0.33 * input_dim), int(0.25 * input_dim))
        self.fc5 = nn.Linear(int(0.25 * input_dim), int(0.33 * input_dim))
        self.fc6 = nn.Linear(int(0.33 * input_dim), int(0.5 * input_dim))
        self.fc7 = nn.Linear(int(0.5 * input_dim), int(0.75 * input_dim))
        self.fc8 = nn.Linear(int(0.75 * input_dim), input_dim)

    def forward(self, x):
        x = torch.tanh(self.fc1(x))
        x = torch.tanh(self.fc2(x))
        x = torch.tanh(self.fc3(x))
        x = torch.tanh(self.fc4(x))
        x = torch.tanh(self.fc5(x))
        x = torch.tanh(self.fc6(x))
        x = torch.tanh(self.fc7(x))
        x = self.fc8(x)
        return torch.softmax(x, dim=1)


# %%
def test_with_data(net, df_malicious, scalar, x_trainer, x_tester, df, features, tr):
    print(f"Calculated threshold is {tr}")
    model = AnomalyModel(net, tr, scalar)
    # %% pandas data grabbing
    df_benign = pd.DataFrame(x_tester, columns=df.columns)
    df_benign["malicious"] = 0
    df_malicious = df_malicious.sample(n=df_benign.shape[0], random_state=17)[list(features)]
    df_malicious["malicious"] = 1
    df = df_benign.append(df_malicious)
    X_test = df.drop(columns=["malicious"]).values
    X_test_scaled = scalar.transform(X_test)
    Y_test = df["malicious"]
    Y_pred = model.predict(torch.from_numpy(X_test_scaled).float())
    # %% printing to console
    printing_press(Y_pred, Y_test)
    # %% writing to lime html files
    # lime_writing(X_test, Y_test, df, model, x_trainer)


# %%
def printing_press(Y_pred, Y_test):
    print(f"Accuracy:\n {accuracy_score(Y_test, Y_pred)}.")
    print(f"Recall:\n {recall_score(Y_test, Y_pred)}.")
    print(f"Precision score:\n {precision_score(Y_test, Y_pred)}.")
    print(f"confusion matrix:\n {confusion_matrix(Y_test, Y_pred)}.")
    print(f"classification report:\n {classification_report(Y_test, Y_pred)}")
    skplt.metrics.plot_confusion_matrix(Y_test,
                                        Y_pred,
                                        title="Multi Worker Test",
                                        text_fontsize="large")
    plt.savefig(
        f"figures/multiWorker/confusionMatrix_{FLAGS.Input_dim}_{FLAGS.Learn_rate}_{FLAGS.Epochs}_{FLAGS.Batch_size}.png")


# %%
def lime_writing(X_test, Y_test, df, model, x_train):
    print("explaining with LIME\n---------------------------------")
    for j in range(5):
        i = np.random.randint(0, X_test.shape[0])
        print(f"Explaining for record nr {i}")
        explainer = lime.lime_tabular.LimeTabularExplainer(
            x_train.values,
            feature_names=df.drop(columns=['malicious']).columns.tolist(),
            discretize_continuous=True)
        exp = explainer.explain_instance(X_test[i], model.scale_predict_classes)
        exp.save_to_file(f"lime_centralized/explanation{j}.html")
        print(exp.as_list())
        print("Actual class")
        print(Y_test.iloc[[i]])
    print("---------------------------------")


# %%
class AnomalyModel:
    def __init__(self, model, threshold, scaler):
        self.model = model
        self.threshold = threshold
        self.scaler = scaler

    def predict(self, x):
        x = x.to(device0)
        x = x.send(tester_hook)
        self.model = self.model.get()
        x_pred = self.model(x)
        if torch.cuda.is_available():
            torch.cuda.synchronize()
        mse = np.mean(np.power(x.cpu().data.numpy() - x_pred.cpu().data.numpy(), 2), axis=1)
        y_pred = mse > self.threshold
        return y_pred.astype(int)

    def scale_predict_classes(self, x):
        x = self.scaler.transform(x)
        y_pred = self.predict(torch.from_numpy(x).float())
        classes_arr = []
        for e in y_pred:
            el = [0, 0]
            el[e] = 1
            classes_arr.append(el)

        return np.array(classes_arr)


# %%
def main(argv):
    if len(argv) > 2:
        raise app.UsageError("Expected one command-line argument(s), "
                             f"got: {argv}")
    # %%
    input_dim = FLAGS.Input_dim
    net = Net(input_dim).to('cpu')
    # %%
    training_data, input_dim, features = get_train_data(input_dim)
    x_train, x_opt, x_test = np.split(
        training_data.sample(frac=1, random_state=1),
        [int(1 / 3 * len(training_data)),
         int(2 / 3 * len(training_data))])
    x_tester = x_test
    x_trainer = x_train
    # %%
    x_train, x_opt, x_test, scalar = create_scalar(x_opt, x_test, x_train)
    # %%
    batch_size = FLAGS.Batch_size
    epochs = FLAGS.Epochs
    learn_rate = FLAGS.Learn_rate
    # %%
    mse = train(net=net,
                x_train=torch.from_numpy(x_train).float(),
                x_opt=torch.from_numpy(x_opt).float(),
                batch_size=batch_size,
                epochs=epochs,
                learn_rate=learn_rate)
    tr = cal_threshold(mse=mse, input_dim=input_dim)
    print(tr)
    # %%
    evaluation(net,
               torch.from_numpy(x_test).float(),
               tr=tr)

    print(f"Testing--------------------")
    test_with_data(net=net, df=training_data,
                   scalar=scalar, x_trainer=x_trainer, x_tester=x_tester,
                   tr=tr, df_malicious=load_mal_data(), features=features)

    os._exit(0)


if __name__ == '__main__':
    app.run(main)