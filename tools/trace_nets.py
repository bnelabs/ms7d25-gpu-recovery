#!/usr/bin/env python3
"""Read the supplied GENCAD netlist; print the independently gated reset path."""
import json,pathlib,sys
p=pathlib.Path(sys.argv[1]) if len(sys.argv)>1 else pathlib.Path(__file__).resolve().parent.parent/'evidence/ms7d25-boardview.cad'
nets={};components={};section='';current=None
for line in p.read_text().splitlines():
    if line.startswith('$'):section=line;continue
    if section=='$SIGNALS':
        if line.startswith('SIGNAL '):current=line[7:];nets[current]=[]
        elif line.startswith('NODE '):nets[current].append(line.split()[1:])
    elif section=='$COMPONENTS':
        if line.startswith('COMPONENT '):current=line[10:];components[current]=[]
        elif current:components[current].append(line)
for net in ('GPP_E2','N269145723','PLTRST_BU1#_X16','PLTRST_BU1#'):
    print(net, json.dumps(nets[net]))
for part in ('UTB1','RTB1','R287','R296'):
    print(part,json.dumps(components[part]))
