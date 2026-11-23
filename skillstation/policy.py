"""Cross-system decision integration."""
from . import candidate,station,assessment
SUBSYSTEMS=('candidate','station','assessment')
def calculate(events):
    c,ce,cd=candidate.calculate(events)
    s,se,sd=station.calculate(events,c)
    a,ae,ad=assessment.calculate(events,c,s)
    return ce+se+ae,cd+sd+ad
