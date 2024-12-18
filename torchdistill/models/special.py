import os

import numpy as np
import torch
from torch import nn
from torch.jit.annotations import Tuple, List

from torchdistill.common.constant import def_logger
from torchdistill.models.classification.vit import Attention, CONFIGS
from torchdistill.models.util import wrap_if_distributed, load_module_ckpt, save_module_ckpt, redesign_model

logger = def_logger.getChild(__name__)
SPECIAL_CLASS_DICT = dict()
from torch import einsum
from einops import rearrange


def register_special_module(cls):
    SPECIAL_CLASS_DICT[cls.__name__] = cls
    return cls


class SpecialModule(nn.Module):
    def __init__(self):
        super().__init__()

    def post_forward(self, *args, **kwargs):
        pass

    def post_process(self, *args, **kwargs):
        pass


@register_special_module
class EmptyModule(SpecialModule):
    def __init__(self, **kwargs):
        super().__init__()

    def forward(self, *args, **kwargs):
        return args[0] if isinstance(args, tuple) and len(args) == 1 else args


class Paraphraser4FactorTransfer(nn.Module):
    """
    Paraphraser for factor transfer described in the supplementary material of
    "Paraphrasing Complex Network: Network Compression via Factor Transfer"
    """

    @staticmethod
    def make_tail_modules(num_output_channels, uses_bn):
        leaky_relu = nn.LeakyReLU(0.1)
        if uses_bn:
            return [nn.BatchNorm2d(num_output_channels), leaky_relu]
        return [leaky_relu]

    @classmethod
    def make_enc_modules(cls, num_input_channels, num_output_channels, kernel_size, stride, padding, uses_bn):
        return [
            nn.Conv2d(num_input_channels, num_output_channels, kernel_size, stride=stride, padding=padding),
            *cls.make_tail_modules(num_output_channels, uses_bn)
        ]

    @classmethod
    def make_dec_modules(cls, num_input_channels, num_output_channels, kernel_size, stride, padding, uses_bn):
        return [
            nn.ConvTranspose2d(num_input_channels, num_output_channels, kernel_size, stride=stride, padding=padding),
            *cls.make_tail_modules(num_output_channels, uses_bn)
        ]

    def __init__(self, k, num_input_channels, kernel_size=3, stride=1, padding=1, uses_bn=True):
        super().__init__()
        self.paraphrase_rate = k
        num_enc_output_channels = int(num_input_channels * k)
        self.encoder = nn.Sequential(
            *self.make_enc_modules(num_input_channels, num_input_channels,
                                   kernel_size, stride, padding, uses_bn),
            *self.make_enc_modules(num_input_channels, num_enc_output_channels,
                                   kernel_size, stride, padding, uses_bn),
            *self.make_enc_modules(num_enc_output_channels, num_enc_output_channels,
                                   kernel_size, stride, padding, uses_bn)
        )
        self.decoder = nn.Sequential(
            *self.make_dec_modules(num_enc_output_channels, num_enc_output_channels,
                                   kernel_size, stride, padding, uses_bn),
            *self.make_dec_modules(num_enc_output_channels, num_input_channels,
                                   kernel_size, stride, padding, uses_bn),
            *self.make_dec_modules(num_input_channels, num_input_channels,
                                   kernel_size, stride, padding, uses_bn)
        )

    def forward(self, z):
        if self.training:
            return self.decoder(self.encoder(z))
        return self.encoder(z)


class Translator4FactorTransfer(nn.Sequential):
    """
    Translator for factor transfer described in the supplementary material of
    "Paraphrasing Complex Network: Network Compression via Factor Transfer"
    Note that "the student translator has the same three convolution layers as the paraphraser"
    """

    def __init__(self, num_input_channels, num_output_channels, kernel_size=3, stride=1, padding=1, uses_bn=True):
        super().__init__(
            *Paraphraser4FactorTransfer.make_enc_modules(num_input_channels, num_input_channels,
                                                         kernel_size, stride, padding, uses_bn),
            *Paraphraser4FactorTransfer.make_enc_modules(num_input_channels, num_output_channels,
                                                         kernel_size, stride, padding, uses_bn),
            *Paraphraser4FactorTransfer.make_enc_modules(num_output_channels, num_output_channels,
                                                         kernel_size, stride, padding, uses_bn)
        )


@register_special_module
class Teacher4FactorTransfer(SpecialModule):
    """
    Teacher for factor transfer proposed in "Paraphrasing Complex Network: Network Compression via Factor Transfer"
    """

    def __init__(self, teacher_model, minimal, input_module_path,
                 paraphraser_params, paraphraser_ckpt, uses_decoder, device, device_ids, distributed, **kwargs):
        super().__init__()
        if minimal is None:
            minimal = dict()

        special_teacher_model = build_special_module(minimal, teacher_model=teacher_model)
        model_type = 'original'
        teacher_ref_model = teacher_model
        if special_teacher_model is not None:
            teacher_ref_model = special_teacher_model
            model_type = type(teacher_ref_model).__name__

        self.teacher_model = redesign_model(teacher_ref_model, minimal, 'teacher', model_type)
        self.input_module_path = input_module_path
        self.paraphraser = \
            wrap_if_distributed(Paraphraser4FactorTransfer(**paraphraser_params), device, device_ids, distributed)
        self.ckpt_file_path = paraphraser_ckpt
        if os.path.isfile(self.ckpt_file_path):
            map_location = {'cuda:0': 'cuda:{}'.format(device_ids[0])} if distributed else device
            load_module_ckpt(self.paraphraser, map_location, self.ckpt_file_path)
        self.uses_decoder = uses_decoder

    def forward(self, *args):
        with torch.no_grad():
            return self.teacher_model(*args)

    def post_forward(self, io_dict):
        if self.uses_decoder and not self.paraphraser.training:
            self.paraphraser.train()
        self.paraphraser(io_dict[self.input_module_path]['output'])

    def post_process(self, *args, **kwargs):
        save_module_ckpt(self.paraphraser, self.ckpt_file_path)


@register_special_module
class Student4FactorTransfer(SpecialModule):
    """
    Student for factor transfer proposed in "Paraphrasing Complex Network: Network Compression via Factor Transfer"
    """

    def __init__(self, student_model, input_module_path, translator_params, device, device_ids, distributed, **kwargs):
        super().__init__()
        self.student_model = wrap_if_distributed(student_model, device, device_ids, distributed)
        self.input_module_path = input_module_path
        self.translator = \
            wrap_if_distributed(Translator4FactorTransfer(**translator_params), device, device_ids, distributed)

    def forward(self, *args):
        return self.student_model(*args)

    def post_forward(self, io_dict):
        self.translator(io_dict[self.input_module_path]['output'])


@register_special_module
class Connector4DAB(SpecialModule):
    """
    Connector proposed in "Knowledge Transfer via Distillation of Activation Boundaries Formed by Hidden Neurons"
    """

    @staticmethod
    def build_connector(conv_params_config, bn_params_config=None):
        module_list = [nn.Conv2d(**conv_params_config)]
        if bn_params_config is not None and len(bn_params_config) > 0:
            module_list.append(nn.BatchNorm2d(**bn_params_config))
        return nn.Sequential(*module_list)

    def __init__(self, student_model, connectors, device, device_ids, distributed, **kwargs):
        super().__init__()
        self.student_model = wrap_if_distributed(student_model, device, device_ids, distributed)
        io_path_pairs = list()
        self.connector_dict = nn.ModuleDict()
        for connector_key, connector_params in connectors.items():
            connector = self.build_connector(connector_params['conv_params'], connector_params.get('bn_params', None))
            self.connector_dict[connector_key] = wrap_if_distributed(connector, device, device_ids, distributed)
            io_path_pairs.append((connector_key, connector_params['io'], connector_params['path']))
        self.io_path_pairs = io_path_pairs

    def forward(self, x):
        return self.student_model(x)

    def post_forward(self, io_dict):
        for connector_key, io_type, module_path in self.io_path_pairs:
            self.connector_dict[connector_key](io_dict[module_path][io_type])


def get_relative_distances(window_size):
    indices = torch.tensor(np.array([[x, y] for x in range(window_size) for y in range(window_size)])).long()
    distances = indices[None, :, :] - indices[:, None, :]
    return distances


class CyclicShift(nn.Module):
    def __init__(self, displacement):
        super().__init__()
        self.displacement = displacement

    def forward(self, x):
        return torch.roll(x, shifts=(self.displacement, self.displacement), dims=(1, 2))


class Residual(nn.Module):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, x, **kwargs):
        return self.fn(x, **kwargs) + x


class PreNorm(nn.Module):
    def __init__(self, dim, fn):
        super().__init__()
        self.norm = nn.LayerNorm(dim)
        self.fn = fn

    def forward(self, x, **kwargs):
        return self.fn(self.norm(x), **kwargs)


class FeedForward(nn.Module):
    def __init__(self, dim, hidden_dim):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, hidden_dim),
            nn.GELU(),
            nn.Linear(hidden_dim, dim),
        )

    def forward(self, x):
        return self.net(x)


def create_mask(window_size, displacement, upper_lower, left_right):
    mask = torch.zeros(window_size ** 2, window_size ** 2)

    if upper_lower:
        mask[-displacement * window_size:, :-displacement * window_size] = float('-inf')
        mask[:-displacement * window_size, -displacement * window_size:] = float('-inf')

    if left_right:
        mask = rearrange(mask, '(h1 w1) (h2 w2) -> h1 w1 h2 w2', h1=window_size, h2=window_size)
        mask[:, -displacement:, :, :-displacement] = float('-inf')
        mask[:, :-displacement, :, -displacement:] = float('-inf')
        mask = rearrange(mask, 'h1 w1 h2 w2 -> (h1 w1) (h2 w2)')

    return mask


class WindowAttention(nn.Module):
    def __init__(self, dim, heads, head_dim, shifted, window_size, relative_pos_embedding):
        super().__init__()
        inner_dim = head_dim * heads

        self.heads = heads
        self.scale = head_dim ** -0.5
        self.window_size = window_size
        self.relative_pos_embedding = relative_pos_embedding
        self.shifted = shifted

        if self.shifted:
            displacement = window_size // 2
            self.cyclic_shift = CyclicShift(-displacement)
            self.cyclic_back_shift = CyclicShift(displacement)
            self.upper_lower_mask = nn.Parameter(create_mask(window_size=window_size, displacement=displacement,
                                                             upper_lower=True, left_right=False), requires_grad=False)
            self.left_right_mask = nn.Parameter(create_mask(window_size=window_size, displacement=displacement,
                                                            upper_lower=False, left_right=True), requires_grad=False)

        self.to_qkv = nn.Linear(dim, inner_dim * 3, bias=False)

        if self.relative_pos_embedding:
            self.relative_indices = get_relative_distances(window_size) + window_size - 1
            self.pos_embedding = nn.Parameter(torch.randn(2 * window_size - 1, 2 * window_size - 1))
        else:
            self.pos_embedding = nn.Parameter(torch.randn(window_size ** 2, window_size ** 2))

        self.to_out = nn.Linear(inner_dim, dim)

    def forward(self, x):
        if self.shifted:
            x = self.cyclic_shift(x)

        b, n_h, n_w, _, h = *x.shape, self.heads

        qkv = self.to_qkv(x).chunk(3, dim=-1)
        nw_h = n_h // self.window_size
        nw_w = n_w // self.window_size

        q, k, v = map(
            lambda t: rearrange(t, 'b (nw_h w_h) (nw_w w_w) (h d) -> b h (nw_h nw_w) (w_h w_w) d',
                                h=h, w_h=self.window_size, w_w=self.window_size), qkv)

        dots = einsum('b h w i d, b h w j d -> b h w i j', q, k) * self.scale

        if self.relative_pos_embedding:
            dots += self.pos_embedding[self.relative_indices[:, :, 0], self.relative_indices[:, :, 1]]
        else:
            dots += self.pos_embedding

        if self.shifted:
            dots[:, :, -nw_w:] += self.upper_lower_mask
            dots[:, :, nw_w - 1::nw_w] += self.left_right_mask

        attn = dots.softmax(dim=-1)

        out = einsum('b h w i j, b h w j d -> b h w i d', attn, v)
        out = rearrange(out, 'b h (nw_h nw_w) (w_h w_w) d -> b (nw_h w_h) (nw_w w_w) (h d)',
                        h=h, w_h=self.window_size, w_w=self.window_size, nw_h=nw_h, nw_w=nw_w)
        out = self.to_out(out)

        if self.shifted:
            out = self.cyclic_back_shift(out)
        return out


class Embed(nn.Module):
    def __init__(self, in_channels=512, out_channels=128, **kwargs):
        super(Embed, self).__init__()
        self.swin = WindowAttention(dim=in_channels,
                                    heads=8,
                                    head_dim=in_channels // 8,
                                    shifted=True,
                                    window_size=7,
                                    relative_pos_embedding=True)
        self.conv2d = nn.Conv2d(in_channels, out_channels, kernel_size=1, stride=1, padding=0, bias=False)
        self.l2norm = nn.BatchNorm2d(out_channels)

    def forward(self, x):
        x = x.permute(0, 2, 3, 1)
        x = self.swin(x)
        x = x.permute(0, 3, 1, 2)
        x = self.conv2d(x)
        x = self.l2norm(x)
        return x


# class Embed(nn.Module):
#     def __init__(self, in_channels=512, out_channels=128, **kwargs):
#         super(Embed, self).__init__()
#         self.conv2d = nn.Conv2d(in_channels, out_channels, kernel_size=3, stride=1, padding=1, bias=False)
#         self.l2norm = nn.BatchNorm2d(out_channels)
#
#     def forward(self, x):
#         x = self.conv2d(x)
#         x = self.l2norm(x)
#         return x


class Regressor4VID(nn.Module):
    def __init__(self, in_channels, middle_channels, out_channels, eps, init_pred_var, **kwargs):
        super().__init__()
        self.regressor = nn.Sequential(
            nn.Conv2d(in_channels, middle_channels, kernel_size=1, stride=1, padding=0, bias=False),
            nn.ReLU(inplace=True),
            nn.Conv2d(middle_channels, middle_channels, kernel_size=1, stride=1, padding=0, bias=False),
            nn.ReLU(inplace=True),
            nn.Conv2d(middle_channels, out_channels, kernel_size=1, stride=1, padding=0, bias=False),
        )
        self.soft_plus_param = \
            nn.Parameter(np.log(np.exp(init_pred_var - eps) - 1.0) * torch.ones(out_channels))
        self.eps = eps
        self.init_pred_var = init_pred_var

    def forward(self, student_feature_map):
        pred_mean = self.regressor(student_feature_map)
        pred_var = torch.log(1.0 + torch.exp(self.soft_plus_param)) + self.eps
        pred_var = pred_var.view(1, -1, 1, 1)
        return pred_mean, pred_var


@register_special_module
class ChannelSimilarityEmbed(SpecialModule):
    """
    """

    def __init__(self, student_model, embedings, device, device_ids, distributed, **kwargs):
        super().__init__()
        self.student_model = wrap_if_distributed(student_model, device, device_ids, distributed)
        io_path_pairs = list()
        self.embed_dict = nn.ModuleDict()
        for embed_key, embed_params in embedings.items():
            embed = Embed(**embed_params)
            self.embed_dict[embed_key] = wrap_if_distributed(embed, device, device_ids, distributed)
            io_path_pairs.append((embed_key, embed_params['io'], embed_params['path']))
        self.io_path_pairs = io_path_pairs

    def forward(self, x):
        return self.student_model(x)

    def post_forward(self, io_dict):
        for embed_key, io_type, module_path in self.io_path_pairs:
            self.embed_dict[embed_key](io_dict[module_path][io_type])


@register_special_module
class AttnEmbed(SpecialModule):
    """
        Embedding functions.
        Put all your learnable parameters in this py file.
    """

    def __init__(self, embedings, device, device_ids, distributed,
                 teacher_model=None, student_model=None, **kwargs):
        super().__init__()
        is_teacher = teacher_model is not None
        self.is_teacher = is_teacher
        if not is_teacher:
            student_model = wrap_if_distributed(student_model, device, device_ids, distributed)

        self.model = teacher_model if is_teacher else student_model

        io_path_pairs = list()
        self.embed_dict = nn.ModuleDict()
        for embed_key, embed_params in embedings.items():
            if is_teacher:
                logger.info("Using {}, compute the key of teacher".format(self.__class__.__name__))
                # embed = Embed(**embed_params) # For ablation study
                # embed = wrap_if_distributed(embed, device, device_ids, distributed)

                embed = nn.Identity()  # no 3x3 conv
            else:
                logger.info(
                    "Using {}, compute the query and attention output of student".format(self.__class__.__name__))
                if 'query' in embed_key:
                    embed = Embed(**embed_params)
                    embed = wrap_if_distributed(embed, device, device_ids, distributed)

                    # embed = nn.Identity() # no 3x3 conv, for ablation
                elif 'value' in embed_key:
                    embed = Embed(**embed_params)
                    embed = wrap_if_distributed(embed, device, device_ids, distributed)
            self.embed_dict[embed_key] = embed
            io_path_pairs.append((embed_key, embed_params['io'], embed_params['path']))
        self.io_path_pairs = io_path_pairs

    def forward(self, x):
        if self.is_teacher:
            with torch.no_grad():
                return self.model(x)
        else:
            return self.model(x)

    def post_forward(self, io_dict):
        for embed_key, io_type, module_path in self.io_path_pairs:
            self.embed_dict[embed_key](io_dict[module_path][io_type])


class AttnModule(nn.Module):
    """
    Calculate the attention for student input
    """

    def __init__(self, vit_type='ViT-B_16', vis=True):
        super(AttnModule, self).__init__()
        vit_config = CONFIGS[vit_type]
        self.n_patches = 197
        self.hidden_size = vit_config.hidden_size
        self.conv_1 = nn.Conv2d(512, 9456, kernel_size=4, stride=1, padding=0, bias=False)
        # self.conv_1 = nn.Conv2d(2048,2048,kernel_size=4,stride=1,padding=0,bias=False)
        # self.conv_2 = nn.Conv2d(2048,9456,kernel_size=1,stride=1,padding=0,bias=False)
        self.attention_norm = torch.nn.LayerNorm(vit_config.hidden_size, eps=1e-6)
        self.attn = Attention(vit_config, vis)

    def forward(self, x):
        x = self.conv_1(x)
        # x = self.conv_2(x)
        x = x.view(-1, self.n_patches, self.hidden_size)
        x = self.attention_norm(x)
        s_out, s_attn_weights = self.attn(x)
        return s_out, s_attn_weights


@register_special_module
class ViTEmbed(SpecialModule):
    """
    Return the attention of the input
    """

    def __init__(self, student_model, embedings, device, device_ids, distributed, **kwargs):
        super().__init__()
        self.student_model = wrap_if_distributed(student_model, device, device_ids, distributed)
        io_path_pairs = list()
        self.embed_dict = nn.ModuleDict()
        for embed_key, embed_params in embedings.items():
            logger.info("Using {}, return the attention of student".format(self.__class__.__name__))
            embed = AttnModule()
            self.embed_dict[embed_key] = wrap_if_distributed(embed, device, device_ids, distributed)
            io_path_pairs.append((embed_key, embed_params['io'], embed_params['path']))
        self.io_path_pairs = io_path_pairs

    def forward(self, x):
        return self.student_model(x)

    def post_forward(self, io_dict):
        for embed_key, io_type, module_path in self.io_path_pairs:
            self.embed_dict[embed_key](io_dict[module_path][io_type])


@register_special_module
class VariationalDistributor4VID(SpecialModule):
    """
    "Variational Information Distillation for Knowledge Transfer"
    """

    def __init__(self, student_model, regressors, device, device_ids, distributed, **kwargs):
        super().__init__()
        self.student_model = wrap_if_distributed(student_model, device, device_ids, distributed)
        io_path_pairs = list()
        self.regressor_dict = nn.ModuleDict()
        for regressor_key, regressor_params in regressors.items():
            regressor = Regressor4VID(**regressor_params)
            self.regressor_dict[regressor_key] = wrap_if_distributed(regressor, device, device_ids, distributed)
            io_path_pairs.append((regressor_key, regressor_params['io'], regressor_params['path']))
        self.io_path_pairs = io_path_pairs

    def forward(self, x):
        return self.student_model(x)

    def post_forward(self, io_dict):
        for regressor_key, io_type, module_path in self.io_path_pairs:
            self.regressor_dict[regressor_key](io_dict[module_path][io_type])


@register_special_module
class Linear4CCKD(SpecialModule):
    """
    Fully-connected layer to cope with a mismatch of feature representations of teacher and student network for
    "Correlation Congruence for Knowledge Distillation"
    """

    def __init__(self, input_module, linear_params, device, device_ids, distributed,
                 teacher_model=None, student_model=None, **kwargs):
        super().__init__()
        is_teacher = teacher_model is not None
        if not is_teacher:
            student_model = wrap_if_distributed(student_model, device, device_ids, distributed)

        self.model = teacher_model if is_teacher else student_model
        self.is_teacher = is_teacher
        self.input_module_path = input_module['path']
        self.input_module_io = input_module['io']
        self.linear = wrap_if_distributed(nn.Linear(**linear_params), device, device_ids, distributed)

    def forward(self, x):
        if self.is_teacher:
            with torch.no_grad():
                return self.model(x)
        return self.model(x)

    def post_forward(self, io_dict):
        flat_outputs = torch.flatten(io_dict[self.input_module_path][self.input_module_io], 1)
        self.linear(flat_outputs)


class Normalizer4CRD(nn.Module):
    def __init__(self, linear, power=2):
        super().__init__()
        self.linear = linear
        self.power = power

    def forward(self, x):
        z = self.linear(x)
        norm = z.pow(self.power).sum(1, keepdim=True).pow(1.0 / self.power)
        out = z.div(norm)
        return out


@register_special_module
class Linear4CRD(SpecialModule):
    """
    "Contrastive Representation Distillation"
    Refactored https://github.com/HobbitLong/RepDistiller/blob/master/crd/memory.py
    """

    def __init__(self, input_module_path, linear_params, device, device_ids, distributed, power=2,
                 teacher_model=None, student_model=None, **kwargs):
        super().__init__()
        is_teacher = teacher_model is not None
        if not is_teacher:
            student_model = wrap_if_distributed(student_model, device, device_ids, distributed)

        self.model = teacher_model if is_teacher else student_model
        self.is_teacher = is_teacher
        self.empty = nn.Sequential()
        self.input_module_path = input_module_path
        linear = nn.Linear(**linear_params)
        self.normalizer = wrap_if_distributed(Normalizer4CRD(linear, power=power), device, device_ids, distributed)

    def forward(self, x, supp_dict):
        # supp_dict is given to be hooked and stored in io_dict
        self.empty(supp_dict)
        if self.is_teacher:
            with torch.no_grad():
                return self.model(x)
        return self.model(x)

    def post_forward(self, io_dict):
        flat_outputs = torch.flatten(io_dict[self.input_module_path]['output'], 1)
        self.normalizer(flat_outputs)


@register_special_module
class HeadRCNN(SpecialModule):
    def __init__(self, head_rcnn, **kwargs):
        super().__init__()
        tmp_ref_model = kwargs.get('teacher_model', None)
        ref_model = kwargs.get('student_model', tmp_ref_model)
        if ref_model is None:
            raise ValueError('Either student_model or teacher_model has to be given.')

        self.transform = ref_model.transform
        self.seq = redesign_model(ref_model, head_rcnn, 'R-CNN', 'HeadRCNN')

    def forward(self, images, targets=None):
        original_image_sizes = torch.jit.annotate(List[Tuple[int, int]], [])
        for img in images:
            val = img.shape[-2:]
            assert len(val) == 2
            original_image_sizes.append((val[0], val[1]))

        images, targets = self.transform(images, targets)
        return self.seq(images.tensors)


@register_special_module
class SSWrapper4SSKD(SpecialModule):
    """
    Semi-supervision wrapper for "Knowledge Distillation Meets Self-Supervision"
    """

    def __init__(self, input_module, feat_dim, ss_module_ckpt, device, device_ids, distributed, freezes_ss_module=False,
                 teacher_model=None, student_model=None, **kwargs):
        super().__init__()
        is_teacher = teacher_model is not None
        if not is_teacher:
            student_model = wrap_if_distributed(student_model, device, device_ids, distributed)

        self.model = teacher_model if is_teacher else student_model
        self.is_teacher = is_teacher
        self.input_module_path = input_module['path']
        self.input_module_io = input_module['io']
        ss_module = nn.Sequential(
            nn.Linear(feat_dim, feat_dim),
            nn.ReLU(inplace=True),
            nn.Linear(feat_dim, feat_dim)
        )
        self.ckpt_file_path = ss_module_ckpt
        if os.path.isfile(self.ckpt_file_path):
            map_location = {'cuda:0': 'cuda:{}'.format(device_ids[0])} if distributed else device
            load_module_ckpt(ss_module, map_location, self.ckpt_file_path)
        self.ss_module = ss_module if is_teacher and freezes_ss_module \
            else wrap_if_distributed(ss_module, device, device_ids, distributed)

    def forward(self, x):
        if self.is_teacher:
            with torch.no_grad():
                return self.model(x)
        return self.model(x)

    def post_forward(self, io_dict):
        flat_outputs = torch.flatten(io_dict[self.input_module_path][self.input_module_io], 1)
        self.ss_module(flat_outputs)

    def post_process(self, *args, **kwargs):
        save_module_ckpt(self.ss_module, self.ckpt_file_path)


@register_special_module
class VarianceBranch4PAD(SpecialModule):
    """
    Variance branch wrapper for "Prime-Aware Adaptive Distillation"
    """

    def __init__(self, student_model, input_module, feat_dim, var_estimator_ckpt,
                 device, device_ids, distributed, **kwargs):
        super().__init__()
        self.student_model = wrap_if_distributed(student_model, device, device_ids, distributed)
        self.input_module_path = input_module['path']
        self.input_module_io = input_module['io']
        var_estimator = nn.Sequential(
            nn.Linear(feat_dim, feat_dim),
            nn.BatchNorm1d(feat_dim)
        )
        self.ckpt_file_path = var_estimator_ckpt
        if os.path.isfile(self.ckpt_file_path):
            map_location = {'cuda:0': 'cuda:{}'.format(device_ids[0])} if distributed else device
            load_module_ckpt(var_estimator, map_location, self.ckpt_file_path)
        self.var_estimator = wrap_if_distributed(var_estimator, device, device_ids, distributed)

    def forward(self, x):
        return self.student_model(x)

    def post_forward(self, io_dict):
        embed_outputs = io_dict[self.input_module_path][self.input_module_io].flatten(1)
        self.var_estimator(embed_outputs)

    def post_process(self, *args, **kwargs):
        save_module_ckpt(self.var_estimator, self.ckpt_file_path)


def get_special_module(class_name, *args, **kwargs):
    if class_name not in SPECIAL_CLASS_DICT:
        logger.info('No special module called `{}` is registered.'.format(class_name))
        return None

    instance = SPECIAL_CLASS_DICT[class_name](*args, **kwargs)
    return instance


def build_special_module(model_config, **kwargs):
    special_model_config = model_config.get('special', dict())
    special_model_type = special_model_config.get('type', None)
    if special_model_type is not None:
        special_model_params_config = special_model_config.get('params', None)
        if special_model_params_config is None:
            special_model_params_config = dict()
        return get_special_module(special_model_type, **kwargs, **special_model_params_config)
    return None
