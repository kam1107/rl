#!/usr/bin/env python
import os
import time
import argparse

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable

import gym

from ptan.common import runfile, env_params, utils, wrappers
from ptan.actions.epsilon_greedy import ActionSelectorEpsilonGreedy
from ptan import experience, agent

import tensorboard_logger as tb

GAMMA = 0.99

REPORT_ITERS = 10


class Net(nn.Module):
    def __init__(self, n_actions, input_shape):
        super(Net, self).__init__()
        self.conv1 = nn.Conv2d(input_shape[0], 32, 5)
        self.conv2 = nn.Conv2d(32, 32, 3)
        self.conv3 = nn.Conv2d(32, 64, 2)

        n_size = self._get_conv_output(input_shape)

        self.fc1 = nn.Linear(n_size, 256)
        self.fc2 = nn.Linear(256, 256)
        self.fc3 = nn.Linear(256, n_actions)

    def _get_conv_output(self, shape):
        input = Variable(torch.rand(1, *shape))
        output_feat = self._forward_conv(input)
        print("Conv out shape: %s" % str(output_feat.size()))
        n_size = output_feat.data.view(1, -1).size(1)
        return n_size

    def _forward_conv(self, x):
        x = F.relu(F.max_pool2d(self.conv1(x), 3))
        x = F.relu(F.max_pool2d(self.conv2(x), 3))
        x = F.relu(F.max_pool2d(self.conv3(x), 3))
        return x

    def forward(self, x):
        x = self._forward_conv(x)
        x = x.view(x.size(0), -1)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = self.fc3(x)
        return x


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-r", "--runfile", required=True, help="Name of the runfile to use")
    parser.add_argument("-m", "--monitor", help="Use monitor and save it's data into given dir")
    parser.add_argument("-s", "--save", help="Directory to save model state")
    parser.add_argument("-n", "--name", required=True, help="Name of the run to log")
    args = parser.parse_args()

    if args.save is not None:
        os.makedirs(args.save, exist_ok=True)

    logdir = os.path.join("logs", args.name)
    os.makedirs(logdir, exist_ok=True)
    tb.configure(logdir)

    run = runfile.RunFile(args.runfile)
    grayscale = run.getboolean("defaults", "grayscale", fallback=True)
    im_width = run.getint("defaults", "image_width", fallback=80)
    im_height = run.getint("defaults", "image_height", fallback=80)
    frames_count = run.getint("defaults", "frames_count", fallback=1)

    def make_env():
        e = gym.make(run.get("defaults", "env"))
        e = wrappers.PreprocessImage(e, height=im_height, width=im_width, grayscale=grayscale)
        if frames_count > 1:
            e = wrappers.FrameBuffer(e, n_frames=frames_count)
        if args.monitor:
            e = gym.wrappers.Monitor(e, args.monitor)
        return e

    env_pool = [make_env() for _ in range(run.getint("defaults", "env_pool_size", fallback=1))]

    params = env_params.EnvParams.from_env(env_pool[0])
    params.load_runfile(run)
    env_params.register(params)

    model = Net(params.n_actions, input_shape=(frames_count if grayscale else 3*frames_count, im_height, im_width))
    if params.cuda_enabled:
        model.cuda()

    loss_fn = nn.MSELoss()
    optimizer = optim.Adam(model.parameters(), lr=run.getfloat("learning", "lr"))

    action_selector = ActionSelectorEpsilonGreedy(epsilon=run.getfloat("defaults", "epsilon"), params=params)
    target_net = agent.TargetNet(model)
    dqn_agent = agent.DQNAgent(dqn_model=model, action_selector=action_selector)
    exp_source = experience.ExperienceSource(env=env_pool, agent=dqn_agent, steps_count=run.getint("defaults", "n_steps"))
    exp_replay = experience.ExperienceReplayBuffer(exp_source, buffer_size=run.getint("exp_buffer", "size"))

    use_target_dqn = run.getboolean("dqn", "target_dqn", fallback=False)
    use_double_dqn = run.getboolean("dqn", "double_dqn", fallback=False)

    if use_target_dqn:
        target_model = target_net.target_model
    else:
        target_model = model

    # running sums of batch values
    batches_count = 0
    batches_sum_q0 = 0.0
    batches_sum_total_reward = 0.0

    def batch_to_train(batch):
        """
        Convert batch into training data using bellman's equation
        :param batch: list of tuples with Experience instances 
        :return: 
        """
        v0_data = []
        vL_data = []

        for exps in batch:
            v0_data.append(exps[0].state)
            vL_data.append(exps[-1].state)

        states_t = torch.from_numpy(np.array(v0_data, dtype=np.float32))
        v0 = Variable(states_t)
        vL = Variable(torch.from_numpy(np.array(vL_data, dtype=np.float32)))
        if params.cuda_enabled:
            v0 = v0.cuda()
            vL = vL.cuda()

        q0 = model(v0).data

        global batches_count, batches_sum_q0, batches_sum_total_reward
        batches_count += 1
        batches_sum_q0 += q0.mean()
        sum_total_reward = 0.0

        if use_target_dqn and use_double_dqn:
            qL = model(vL)
            actions = qL.data.cpu().max(1)[1].squeeze().numpy()
            qL = target_model(vL).data.cpu().numpy()
            total_rewards = qL[range(qL.shape[0]), actions]
        # only target is in use: use best value from it
        elif use_target_dqn:
            q = target_model(vL)
            total_rewards = q.data.max(1)[0].squeeze().cpu().numpy()
        else:
            q = model(vL)
            total_rewards = q.data.max(1)[0].squeeze().cpu().numpy()
        for idx, exps in enumerate(batch):
            # game is done, no final reward
            if exps[-1].done:
                total_reward = 0.0
            else:
                total_reward = total_rewards[idx]
            for exp in reversed(exps[:-1]):
                total_reward = exp.reward + GAMMA * total_reward
            q0[idx][exps[0].action] = total_reward
            sum_total_reward += total_reward
        batches_sum_total_reward += sum_total_reward / len(batch)
        return states_t, q0

    reward_sma = utils.SMAQueue(run.getint("stop", "mean_games", fallback=100))
    speed_mon = utils.SpeedMonitor(run.getint("learning", "batch_size"))

    try:
        for idx in range(10000):
            if run.getboolean("defaults", "reload_config", fallback=False):
                run.check_and_reload()
            exp_replay.populate(run.getint("exp_buffer", "populate"))

            losses = []
            for batch in exp_replay.batches(run.getint("learning", "batch_size")):
                optimizer.zero_grad()

                # populate buffer
                states, q_vals = batch_to_train(batch)
                # ready to train
                states, q_vals = Variable(states), Variable(q_vals)
                if params.cuda_enabled:
                    states = states.cuda()
                    q_vals = q_vals.cuda()
                l = loss_fn(model(states), q_vals)
                losses.append(l.data.cpu().numpy())
                l.backward()
                optimizer.step()
                speed_mon.batch()

            action_selector.epsilon *= run.getfloat("defaults", "epsilon_decay")
            if run.has_option("defaults", "epsilon_minimum"):
                action_selector.epsilon = max(run.getfloat("defaults", "epsilon_minimum"),
                                              action_selector.epsilon)

            # lr decay
            if run.has_option("learning", "lr_decay"):
                lr = None
                for param_group in optimizer.param_groups:
                    param_group['lr'] *= run.getfloat("learning", "lr_decay")
                    if run.has_option("learning", "lr_minimum"):
                        param_group['lr'] = max(param_group['lr'], run.getfloat("learning", "lr_minimum"))
                    lr = param_group['lr']
                tb.log_value("lr", lr, step=idx)

            tb.log_value("qvals_mean", batches_sum_q0 / batches_count, step=idx)
            tb.log_value("qvals_target", batches_sum_total_reward / batches_count, step=idx)
            batches_count = 0
            batches_sum_total_reward = batches_sum_q0 = 0.0

            tb.log_value("loss", np.mean(losses), step=idx)
            tb.log_value("epsilon", action_selector.epsilon, step=idx)

            if idx % REPORT_ITERS == 0:
                total_rewards = exp_source.pop_total_rewards()
                reward_sma += total_rewards
                mean_reward = reward_sma.mean()
                tb.log_value("reward_mean", mean_reward, step=idx)
                mean_reward_str = "%.2f" % mean_reward if mean_reward is not None else 'None'
                print("%d: Mean reward: %s, done: %d, epsilon: %.4f, samples/s: %.3f, epoch time: %s" % (
                    idx, mean_reward_str, len(total_rewards), action_selector.epsilon,
                    speed_mon.samples_per_sec(),
                    speed_mon.epoch_time()
                ))

                if run.has_option("stop", "mean_reward") and mean_reward is not None:
                    if mean_reward >= run.getfloat("stop", "mean_reward"):
                        print("We've reached mean reward bound, exit")
                        break
                speed_mon.reset()

            if idx > 0 and run.has_option("default", "save_epoches"):
                if idx % run.getint("default", "save_epoches") == 0:
                    if args.save:
                        path = os.path.join(args.save, "model-%05d.dat" % idx)
                        with open(path, 'wb') as fd:
                            torch.save(model.state_dict(), fd)
                        print("Model %s saved" % path)
            if idx % run.getint("dqn", "copy_target_net_every_epoch") == 0:
                target_net.sync()
            speed_mon.epoch()
    finally:
        for env in env_pool:
            env.close()


    pass
