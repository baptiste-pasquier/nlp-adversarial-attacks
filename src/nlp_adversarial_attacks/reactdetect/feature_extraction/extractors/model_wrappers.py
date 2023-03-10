import numpy as np
import torch
from transformers import GPT2Model
from transformers.models.bert.modeling_bert import BertEmbeddings
from transformers.models.camembert.modeling_camembert import CamembertEmbeddings
from transformers.models.roberta.modeling_roberta import RobertaEmbeddings


class NodeActivationWrapper(torch.nn.Module):
    """
    Model that adds a forward hook to each layer
    to obtain activations of all internal nodes
    from a forward pass of the model.
    """

    def __init__(self, model):
        super().__init__()
        assert isinstance(model, torch.nn.Module)
        self.model = model
        self.activation_hook = Hook()

    def activation(self, text):
        """
        Do forward pass and collect node activations.
        """
        self._add_hooks()  # add forward hooks

        self.model(text)  # do forward pass to trigger forward hooks
        result = self.activation_hook.values  # collect activations

        self.activation_hook.clear()  # clear hook values
        self._clear_hooks()  # remove handles

        return result

    # private
    def _add_hooks(self):
        """
        Register a hook for each layer.
        """
        self.handles = []
        for _name, module in self._get_individual_modules():
            self.handles.append(module.register_forward_hook(self.activation_hook))

    def _clear_hooks(self):
        """
        Remove hooks.
        """
        for handle in self.handles:
            handle.remove()
        self.handles = []

    def _get_individual_modules(self):
        """
        Returns all individual (not grouped) modules in the network.
        """
        assert isinstance(self.model, torch.nn.Module)

        result = []
        for name, module in self.model.named_modules():
            has_child = False

            for _child in module.children():
                has_child = True

            if not has_child:
                result.append((name, module))

        return result


class SaliencyWrapper(torch.nn.Module):
    """
    Model wrapper that adds the following:
        forward hook to the embedding layer to obtain token embeddings;
        backward hook to the embedding layer to obtain gradients w.r.t token embeddings;
    The values returned from these hooks are then used to compute
    saliency features such as "simple gradients", "integrated gradients", etc.
    """

    def __init__(self, model, embedding_layer=None, saliency_type="simple_gradient"):
        super().__init__()
        assert isinstance(model, torch.nn.Module)
        self.model = model
        self.saliency_type = saliency_type
        self.handles = []
        # make sure given embedding layer is a PyTorch module
        if embedding_layer is not None:
            assert isinstance(embedding_layer, torch.nn.Module)
        # attempt to find the embedding layer if not already given one
        else:
            for module in self.model.modules():
                if isinstance(
                    module, (BertEmbeddings, RobertaEmbeddings, CamembertEmbeddings)
                ):
                    embedding_layer = module.word_embeddings
                elif isinstance(module, GPT2Model):
                    embedding_layer = module.wte
        # throw error if embedding layer is not available
        if embedding_layer is None:
            raise ValueError("embedding layer cannot be found!")
        else:
            self.embedding_layer = embedding_layer

    def saliency(self, text, label, loss_fn=None):
        """
        Input: str, text to compute the gradients with respect to;
               Torch.tensor, tensor containing the label for this sample.
        Returns: 1D Torch.tensor containing a normalized attribution values
                 for each token in the text input.
        """
        if loss_fn is None:
            loss_fn = torch.nn.CrossEntropyLoss()
        label = label.to(self.model.device)
        # compute simple gradients
        if self.saliency_type == "simple_gradient":
            saliency = self._simple_gradient(text, label, loss_fn)
        # compute integrated gradients
        elif self.saliency_type == "integrated_gradient":
            saliency = self._integrated_gradient(text, label, loss_fn)
        # compute integrated gradients
        elif self.saliency_type == "smooth_gradient":
            saliency = self._smooth_gradient(text, label, loss_fn)
        return saliency

    # private
    def _clear_hooks(self):
        for handle in self.handles:
            handle.remove()

    def _simple_gradient(self, text, label, loss_fn):
        """
        Computes "simple gradient" attributions for each token.
        Refernce: https://github.com/allenai/allennlp/blob/master/\
                  allennlp/interpret/saliency_interpreters/simple_gradient.py
        """
        # data containers
        embeddings_hook = Hook()
        gradients_hook = Hook()
        # add hooks to obtain token embeddings and gradients
        self.handles.append(self.embedding_layer.register_forward_hook(embeddings_hook))
        self.handles.append(self.embedding_layer.register_backward_hook(gradients_hook))
        self.model.zero_grad()  # reset gradients
        pred = self.model.forward(text)  # forward pass
        loss = loss_fn(pred, label)  # compute loss
        loss.backward()  # backward pass
        # extract embeddings and gradients (gradients come in reverse order)
        embeddings = embeddings_hook.values_[0][0]
        # print(embeddings.shape)

        gradients = gradients_hook.values_[0][0][0].flip(dims=[0])

        # print(gradients.shape)
        # normalize

        # no embeddings per token, just one embedding vector
        emb_grad = torch.sum(embeddings * gradients, axis=1)

        # embeddings * gradients #prev

        # print(emb_grad.shape)
        norm = torch.linalg.norm(emb_grad, ord=1).item()
        # print(norm)
        saliency = torch.tensor([(torch.abs(e) / norm).item() for e in emb_grad])
        # clean up
        self._clear_hooks()
        return saliency

    def _integrated_gradient(self, text, label, loss_fn, steps=10):
        """
        Computes "integrated gradient" attributions for each token.
        Refernce: https://github.com/allenai/allennlp/blob/master/\
                  allennlp/interpret/saliency_interpreters/integrated_gradient.py
        """
        embeddings_list = []  # container to store the original embeddings
        gradient_sum = None  # stores the running total of gradients
        # approximate integration by summing over a finte number of steps
        for i, alpha in enumerate(np.linspace(0.1, 1.0, num=steps, endpoint=True)):
            # custom hook to modify embeddings on the forward pass
            def custom_forward_hook(module, module_in, module_out):
                # save original embeddings
                if i == 0:  # noqa: B023
                    embeddings_list.append(module_out)
                # modify embeddings to generate different gradients
                module_out.mul_(alpha)  # noqa: B023

            # data container
            gradients_hook = Hook()
            # add hooks to obtain token embeddings and gradients
            self.handles.append(
                self.embedding_layer.register_forward_hook(custom_forward_hook)
            )
            self.handles.append(
                self.embedding_layer.register_backward_hook(gradients_hook)
            )
            self.model.zero_grad()  # reset gradients
            pred = self.model.forward(text)  # forward pass
            loss = loss_fn(pred, label)  # compute loss
            loss.backward()  # backward pass
            # extract modified gradients (these come in reverse order)
            gradients = gradients_hook.values_[0][0][0].flip(dims=[0])
            # keep a running sum of the modified gradients
            if gradient_sum is None:
                gradient_sum = gradients
            else:
                gradient_sum += gradients
            # remove hooks
            self._clear_hooks()
        embeddings = embeddings_list[0][0]  # extract original embeddings
        gradients = gradient_sum / steps  # compute average gradients
        # normalize
        emb_grad = embeddings * gradients
        norm = torch.linalg.norm(emb_grad, ord=1)
        saliency = torch.tensor([(torch.abs(e) / norm).item() for e in emb_grad])
        return saliency

    def _smooth_gradient(self, text, label, loss_fn, std_dev=0.01, num_samples=10):
        """
        Computes "smooth gradient" attributions for each token.
        Refernce: https://github.com/allenai/allennlp/blob/master/allennlp/\
                  interpret/saliency_interpreters/smooth_gradient.py
        """
        embeddings_list = []  # container to store the original embeddings
        gradient_sum = None  # stores the running total of gradients
        # approximate integration by summing over a finte number of steps
        for i in range(num_samples):
            # add random noise to the embeddings
            def custom_forward_hook(module, module_in, module_out):
                # save original embeddings
                if i == 0:  # noqa: B023
                    embeddings_list.append(module_out)
                # random noise = N(0, stdev * (max - min))
                scale = module_out.detach().max() - module_out.detach().min()
                noise = (
                    torch.randn(module_out.shape, device=module_out.device)
                    * std_dev
                    * scale
                )
                # add the random noise
                module_out.add_(noise)

            # data container
            gradients_hook = Hook()
            # add hooks to obtain token embeddings and gradients
            self.handles.append(
                self.embedding_layer.register_forward_hook(custom_forward_hook)
            )
            self.handles.append(
                self.embedding_layer.register_backward_hook(gradients_hook)
            )
            self.model.zero_grad()  # reset gradients
            pred = self.model.forward(text)  # forward pass
            loss = loss_fn(pred, label)  # compute loss
            loss.backward()  # backward pass
            # extract modified gradients (these come in reverse order)
            gradients = gradients_hook.values_[0][0][0].flip(dims=[0])
            # keep a running sum of the modified gradients
            if gradient_sum is None:
                gradient_sum = gradients
            else:
                gradient_sum += gradients
            # remove hooks
            self._clear_hooks()
        embeddings = embeddings_list[0][0]  # extract original embeddings
        gradients = gradient_sum / num_samples  # compute average gradients
        # normalize
        emb_grad = embeddings * gradients
        norm = torch.linalg.norm(emb_grad, ord=1)
        saliency = torch.tensor([(torch.abs(e) / norm).item() for e in emb_grad])
        return saliency


class Hook:
    """
    Object to store output from registered hooks.
    """

    def __init__(self, output=True, custom_fn=None):
        self.output = output
        self.custom_fn = custom_fn
        self.values_ = []

    def __call__(self, module, module_in, module_out):
        if self.output:
            result = module_out
        else:
            result = module_in
        self.values_.append(result)

    def clear(self):
        self.values_ = []

    @property
    def values(self):
        return self.values_
