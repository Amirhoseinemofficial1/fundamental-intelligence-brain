from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(slots=True)
class TemporalArtifact:
    family: str
    model: Any
    sequence_length: int
    feature_count: int

    def predict(self, sequences: Sequence[Sequence[Sequence[float]]]) -> list[float]:
        import torch
        self.model.eval()
        with torch.no_grad():
            tensor=torch.tensor(sequences,dtype=torch.float32)
            return [float(x) for x in torch.sigmoid(self.model(tensor).reshape(-1)).tolist()]


class TemporalModelFactory:
    """Optional real GRU/LSTM/TCN/Transformer adapters.

    PyTorch is intentionally optional.  Importing the core package never requires
    it; callers receive an explicit availability result and can keep using the
    chronological tabular factory.
    """

    FAMILIES=("gru","lstm","tcn","temporal_transformer")

    @staticmethod
    def availability() -> dict[str, Any]:
        try:
            import torch
            return {"available":True,"backend":"torch","version":torch.__version__,"families":list(TemporalModelFactory.FAMILIES)}
        except ImportError:
            return {"available":False,"backend":"torch","reason":"optional_dependency_missing","install_extra":"temporal","families":list(TemporalModelFactory.FAMILIES)}

    @staticmethod
    def _model(family: str, feature_count: int, hidden: int):
        import torch
        from torch import nn
        if family in {"gru","lstm"}:
            recurrent=(nn.GRU if family=="gru" else nn.LSTM)(feature_count,hidden,batch_first=True)
            class Recurrent(nn.Module):
                def __init__(self):super().__init__();self.recurrent=recurrent;self.output=nn.Linear(hidden,1)
                def forward(self,x):
                    sequence,_=self.recurrent(x);return self.output(sequence[:,-1,:])
            return Recurrent()
        if family=="tcn":
            class TCN(nn.Module):
                def __init__(self):super().__init__();self.network=nn.Sequential(nn.Conv1d(feature_count,hidden,3,padding=2,dilation=1),nn.ReLU(),nn.Conv1d(hidden,hidden,3,padding=4,dilation=2),nn.ReLU());self.output=nn.Linear(hidden,1)
                def forward(self,x):return self.output(self.network(x.transpose(1,2))[:,:,-1])
            return TCN()
        if family=="temporal_transformer":
            heads=1
            class Transformer(nn.Module):
                def __init__(self):super().__init__();self.input=nn.Linear(feature_count,hidden);self.encoder=nn.TransformerEncoder(nn.TransformerEncoderLayer(hidden,heads,batch_first=True,dropout=0.0),num_layers=1);self.output=nn.Linear(hidden,1)
                def forward(self,x):return self.output(self.encoder(self.input(x))[:,-1,:])
            return Transformer()
        raise ValueError(f"unknown temporal family {family}")

    def train(self, family: str, sequences: Sequence[Sequence[Sequence[float]]], labels: Sequence[int], epochs: int=20, seed: int=53) -> TemporalArtifact:
        if family not in self.FAMILIES:raise ValueError(f"unknown temporal family {family}")
        if len(sequences)<20 or len(sequences)!=len(labels):raise ValueError("minimum 20 aligned sequences required")
        import torch
        torch.manual_seed(seed);feature_count=len(sequences[0][0]);sequence_length=len(sequences[0]);model=self._model(family,feature_count,max(4,min(32,feature_count*2)));tensor=torch.tensor(sequences,dtype=torch.float32);target=torch.tensor(labels,dtype=torch.float32).reshape(-1,1);optimizer=torch.optim.Adam(model.parameters(),lr=.01);loss_fn=torch.nn.BCEWithLogitsLoss()
        model.train()
        for _ in range(max(1,epochs)):
            optimizer.zero_grad();loss=loss_fn(model(tensor),target);loss.backward();optimizer.step()
        return TemporalArtifact(family,model,sequence_length,feature_count)
