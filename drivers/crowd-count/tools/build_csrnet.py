import torch, torch.nn as nn
def make_layers(cfg, in_channels=3, dilation=False):
    d = 2 if dilation else 1; layers=[]
    for v in cfg:
        if v=='M': layers += [nn.MaxPool2d(2,2)]
        else:
            layers += [nn.Conv2d(in_channels,v,3,padding=d,dilation=d), nn.ReLU(inplace=True)]; in_channels=v
    return nn.Sequential(*layers)
class CSRNet(nn.Module):
    def __init__(self):
        super().__init__()
        self.frontend = make_layers([64,64,'M',128,128,'M',256,256,256,'M',512,512,512])
        self.backend  = make_layers([512,512,512,256,128,64], in_channels=512, dilation=True)
        self.output_layer = nn.Conv2d(64,1,kernel_size=1)
    def forward(self,x):
        return self.output_layer(self.backend(self.frontend(x)))
m=CSRNet().eval()
sd=torch.load("/w/weights.pth", map_location="cpu")
if isinstance(sd,dict) and "state_dict" in sd: sd=sd["state_dict"]
sd={k.replace("module.",""):v for k,v in sd.items()}
r=m.load_state_dict(sd, strict=False)
print("missing keys:",len(r.missing_keys), r.missing_keys[:4])
print("unexpected keys:",len(r.unexpected_keys), r.unexpected_keys[:4])
# sanity: blank vs noise
import numpy as np
def count(img):
    with torch.no_grad(): return float(m(torch.from_numpy(img).unsqueeze(0)).sum().item())
blank=np.full((3,384,512),0.0,"float32")
print("blank sum:", round(count(blank),2))
# trace → TorchScript
ex=torch.randn(1,3,384,512)
tr=torch.jit.trace(m, ex); tr.save("/out/crowd-density.pt")
print("saved TorchScript /out/crowd-density.pt")
# reload traced + verify
t2=torch.jit.load("/out/crowd-density.pt")
with torch.no_grad(): print("traced blank sum:", round(float(t2(torch.from_numpy(blank).unsqueeze(0)).sum().item()),2))
