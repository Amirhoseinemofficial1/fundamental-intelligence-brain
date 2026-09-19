"""Small shared-representation neural contextual bandit for delayed paper rewards.

The network predicts net R for each barrier arm, not a success probability.
It learns only after the outcome is observed and remains shadow-only.
"""
import math
import random

FEATURES=(('event_importance','importance'),('event_novelty','novelty'),('source_quality','source_credibility'),
          ('surprise','surprise'),('priced_in_score','priced_in_score'),('quality','data_quality'),
          ('volatility','volatility'),('regime_probability','regime_probability'),('risk_score','risk_score'),
          ('mapping_confidence','mapping_confidence'))
SEMANTIC_DIM=64
INPUT_DIM=len(FEATURES)+2+SEMANTIC_DIM
HIDDEN_DIM=32


def vector(values, side, horizon):
    def safe(value):
        try:
            value=float(value)
            return math.tanh(value) if math.isfinite(value) else 0.
        except (TypeError,ValueError):return 0.
    base=[safe(values.get(primary,values.get(fallback,0))) for primary,fallback in FEATURES]
    semantic=[safe(values.get(f'semantic_{index:02d}',0)) for index in range(SEMANTIC_DIM)]
    return base+[float(side),math.log1p(max(0,horizon))/16]+semantic


class NeuralBandit:
    def __init__(self,state=None):
        if state:
            self.state=state
            self._upgrade()
        else:
            rng=random.Random(571)
            self.state=dict(version=0,samples=0,
                architecture={'input':INPUT_DIM,'hidden':HIDDEN_DIM,'heads':7,'semantic_dimensions':SEMANTIC_DIM},
                hidden=[[rng.uniform(-.08,.08) for _ in range(INPUT_DIM)] for _ in range(HIDDEN_DIM)],
                hidden_bias=[0.]*HIDDEN_DIM,output=[[rng.uniform(-.08,.08) for _ in range(HIDDEN_DIM)] for _ in range(7)],
                output_bias=[0.]*7,arm_samples=[0]*7)

    def _upgrade(self):
        rng=random.Random(571+int(self.state.get('version',0)))
        hidden=self.state.get('hidden') or []
        for row in hidden:
            row.extend(rng.uniform(-.02,.02) for _ in range(max(0,INPUT_DIM-len(row))))
            del row[INPUT_DIM:]
        while len(hidden)<HIDDEN_DIM:hidden.append([rng.uniform(-.02,.02) for _ in range(INPUT_DIM)])
        del hidden[HIDDEN_DIM:]
        bias=self.state.setdefault('hidden_bias',[]);bias.extend([0.]*(HIDDEN_DIM-len(bias)));del bias[HIDDEN_DIM:]
        output=self.state.setdefault('output',[])
        while len(output)<7:output.append([rng.uniform(-.02,.02) for _ in range(HIDDEN_DIM)])
        del output[7:]
        for row in output:row.extend(rng.uniform(-.02,.02) for _ in range(HIDDEN_DIM-len(row)));del row[HIDDEN_DIM:]
        self.state.setdefault('output_bias',[0.]*7);self.state.setdefault('arm_samples',[0]*7)
        self.state['architecture']={'input':INPUT_DIM,'hidden':HIDDEN_DIM,'heads':7,'semantic_dimensions':SEMANTIC_DIM}

    def forward(self,x):
        h=[math.tanh(sum(w*v for w,v in zip(weights,x))+b) for weights,b in zip(self.state['hidden'],self.state['hidden_bias'])]
        q=[sum(w*v for w,v in zip(weights,h))+b for weights,b in zip(self.state['output'],self.state['output_bias'])]
        return h,q

    def update(self,x,arm,reward):
        if len(x)!=INPUT_DIM or arm not in range(7) or not all(math.isfinite(v) for v in [*x,reward]):raise ValueError('invalid delayed feedback')
        h,q=self.forward(x);target=max(-3.,min(3.,reward));error=max(-3.,min(3.,q[arm]-target))
        rate=.015/math.sqrt(1+self.state['samples']/500)
        old=list(self.state['output'][arm])
        for j in range(HIDDEN_DIM):
            self.state['output'][arm][j]-=rate*(error*h[j]+.0001*old[j])
            dh=error*old[j]*(1-h[j]*h[j])
            for k in range(INPUT_DIM):self.state['hidden'][j][k]-=rate*(dh*x[k]+.0001*self.state['hidden'][j][k])
            self.state['hidden_bias'][j]-=rate*dh
        self.state['output_bias'][arm]-=rate*error
        self.state['samples']+=1;self.state['arm_samples'][arm]+=1;self.state['version']+=1
        return error*error
