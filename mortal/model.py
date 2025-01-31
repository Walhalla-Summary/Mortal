import torch
from torch import nn
from torch.nn import functional as F
from torch.nn.utils.rnn import pack_padded_sequence, pad_sequence
from typing import *
from itertools import permutations
from libriichi.consts import obs_shape, oracle_obs_shape, ACTION_SPACE, GRP_SIZE
from common import apply_masks

class ChannelAttention(nn.Module):
    def __init__(self, channels, ratio=16, actv_builder=nn.ReLU):
        super().__init__()
        self.avg = nn.AdaptiveAvgPool1d(1)
        self.max = nn.AdaptiveMaxPool1d(1)
        self.shared_mlp = nn.Sequential(
            nn.Linear(channels, channels // ratio),
            actv_builder(),
            nn.Linear(channels // ratio, channels),
        )

    def forward(self, x):
        avg_out = self.avg(x).squeeze(-1)
        max_out = self.max(x).squeeze(-1)
        avg_out = self.shared_mlp(avg_out)
        max_out = self.shared_mlp(max_out)
        out = torch.sigmoid(avg_out + max_out).unsqueeze(-1)
        return out

class ResBlock(nn.Module):
    def __init__(self, channels, *, norm_builder=nn.Identity, actv_builder=nn.ReLU, pre_actv=False, bias=True):
        super().__init__()
        self.actv = actv_builder()
        self.pre_actv = pre_actv

        if pre_actv:
            self.res_unit = nn.Sequential(
                norm_builder(),
                actv_builder(),
                nn.Conv1d(channels, channels, kernel_size=3, padding=1, bias=bias),
                norm_builder(),
                actv_builder(),
                nn.Conv1d(channels, channels, kernel_size=3, padding=1, bias=bias),
            )
        else:
            self.res_unit = nn.Sequential(
                nn.Conv1d(channels, channels, kernel_size=3, padding=1, bias=bias),
                norm_builder(),
                actv_builder(),
                nn.Conv1d(channels, channels, kernel_size=3, padding=1, bias=bias),
                norm_builder(),
            )
        self.ca = ChannelAttention(channels, actv_builder=actv_builder)

    def forward(self, x):
        out = self.res_unit(x)
        out = self.ca(out) * out
        out = out + x
        if not self.pre_actv:
            out = self.actv(out)
        return out

class ResNet(nn.Module):
    def __init__(
        self,
        in_channels,
        conv_channels,
        num_blocks,
        *,
        norm_builder = nn.Identity,
        actv_builder = nn.ReLU,
        pre_actv = False,
        bias = True,
        reduce_dim = False,
    ):
        super().__init__()

        blocks = []
        for _ in range(num_blocks):
            blocks.append(ResBlock(
                conv_channels,
                norm_builder = norm_builder,
                actv_builder = actv_builder,
                pre_actv = pre_actv,
                bias = bias,
            ))

        layers = [nn.Conv1d(in_channels, conv_channels, kernel_size=3, padding=1, bias=bias)]
        if pre_actv:
            layers += [*blocks, norm_builder(), actv_builder()]
        else:
            layers += [norm_builder(), actv_builder(), *blocks]
        if reduce_dim:
            layers += [
                nn.Conv1d(conv_channels, 32, kernel_size=3, padding=1),
                actv_builder(),
                nn.Flatten(),
                nn.Linear(32 * 34, 1024),
            ]
        self.net = nn.Sequential(*layers)

    def forward(self, x):
        return self.net(x)

class Brain(nn.Module):
    def __init__(self, *, conv_channels, num_blocks, is_oracle=False, version=1):
        super().__init__()
        self.is_oracle = is_oracle
        self.version = version
        norm_builder = lambda: nn.BatchNorm1d(conv_channels, momentum=0.01)
        bias = False

        match version:
            case 1:
                actv_builder = lambda: nn.ReLU(inplace=True)
                pre_actv = False
                self.latent_net = nn.Sequential(
                    nn.Linear(1024, 512),
                    nn.ReLU(inplace=True),
                )
                self.mu_head = nn.Linear(512, 512)
                self.logsig_head = nn.Linear(512, 512)
            case 2:
                actv_builder = lambda: nn.Mish(inplace=True)
                pre_actv = True

        in_channels = obs_shape(version)[0]
        if is_oracle:
            in_channels += oracle_obs_shape(version)[0]

        self.encoder = ResNet(
            in_channels = in_channels,
            conv_channels = conv_channels,
            num_blocks = num_blocks,
            norm_builder = norm_builder,
            actv_builder = actv_builder,
            pre_actv = pre_actv,
            bias = bias,
            reduce_dim = True,
        )

        # when True, never updates running stats, weights and bias and always use EMA or CMA
        self._freeze_bn = False

    def forward(self, obs, invisible_obs: Optional[torch.Tensor] = None):
        if self.is_oracle:
            assert invisible_obs is not None
            obs = torch.cat((obs, invisible_obs), dim=1)
        phi = self.encoder(obs)

        match self.version:
            case 1:
                latent_out = self.latent_net(phi)
                mu = self.mu_head(latent_out)
                logsig = self.logsig_head(latent_out)
                return mu, logsig
            case 2:
                return F.mish(phi)

    def train(self, mode=True):
        super().train(mode)
        if self._freeze_bn:
            for module in self.modules():
                if isinstance(module, nn.BatchNorm1d):
                    module.eval()
                    # I don't think this benefits
                    # module.requires_grad_(False)
        return self

    def set_track_running_stats(self, value: bool):
        for module in self.modules():
            if isinstance(module, nn.BatchNorm1d):
                module.track_running_stats = value

    def reset_running_stats(self):
        for module in self.modules():
            if isinstance(module, nn.BatchNorm1d):
                module.reset_running_stats()

    def freeze_bn(self, value: bool):
        self._freeze_bn = value
        return self.train(self.training)

class DQN(nn.Module):
    def __init__(self, version=1):
        super().__init__()
        self.version = version
        match version:
            case 1:
                self.v_head = nn.Linear(512, 1)
                self.a_head = nn.Linear(512, ACTION_SPACE)
            case 2:
                self.v_head = nn.Sequential(
                    nn.Linear(1024, 512),
                    nn.Mish(inplace=True),
                    nn.Linear(512, 1),
                )
                self.a_head = nn.Sequential(
                    nn.Linear(1024, 512),
                    nn.Mish(inplace=True),
                    nn.Linear(512, ACTION_SPACE),
                )

    def forward(self, x, mask):
        v = self.v_head(x)
        a = self.a_head(x)

        a_sum = apply_masks(a, mask, fill=0.).sum(-1, keepdim=True)
        mask_sum = mask.sum(-1, keepdim=True)
        a_mean = a_sum / mask_sum
        q = apply_masks(v + a - a_mean, mask)
        return q

class GRP(nn.Module):
    def __init__(self, hidden_size=64, num_layers=2):
        super().__init__()
        self.rnn = nn.GRU(input_size=GRP_SIZE, hidden_size=hidden_size, num_layers=num_layers, batch_first=True)
        self.fc = nn.Sequential(
            nn.Linear(hidden_size * num_layers, hidden_size * num_layers),
            nn.ReLU(inplace=True),
            nn.Linear(hidden_size * num_layers, 24),
        )
        for mod in self.modules():
            mod.to(torch.float64)

        # perms are the permutations of all possible rank-by-player result
        perms = torch.tensor(list(permutations(range(4))))
        perms_t = perms.transpose(0, 1)
        self.register_buffer('perms', perms)     # (24, 4)
        self.register_buffer('perms_t', perms_t) # (4, 24)

    # input: [grand_kyoku, honba, kyotaku, s[0], s[1], s[2], s[3]]
    # grand_kyoku: E1 = 0, S4 = 7, W4 = 11
    # s is 2.5 at E1
    # s[0] is score of player id 0
    def forward(self, inputs):
        lengths = torch.tensor([t.shape[0] for t in inputs], dtype=torch.int64)
        inputs = pad_sequence(inputs, batch_first=True)
        packed_inputs = pack_padded_sequence(inputs, lengths, batch_first=True, enforce_sorted=False)
        return self.forward_packed(packed_inputs)

    def forward_packed(self, packed_inputs):
        _, state = self.rnn(packed_inputs)
        state = state.transpose(0, 1).flatten(1)
        logits = self.fc(state)
        return logits

    # (N, 24) -> (N, player, rank_prob)
    def calc_matrix(self, logits):
        batch_size = logits.shape[0]
        probs = logits.softmax(-1)
        matrix = torch.zeros(batch_size, 4, 4, dtype=probs.dtype)
        for player in range(4):
            for rank in range(4):
                cond = self.perms_t[player] == rank
                matrix[:, player, rank] = probs[:, cond].sum(-1)
        return matrix

    # (N, 4) -> (N)
    def get_label(self, rank_by_player):
        batch_size = rank_by_player.shape[0]
        perms = self.perms.expand(batch_size, -1, -1).transpose(0, 1)
        mappings = (perms == rank_by_player).all(-1).nonzero()

        labels = torch.zeros(batch_size, dtype=torch.int64, device=mappings.device)
        labels[mappings[:, 1]] = mappings[:, 0]
        return labels
