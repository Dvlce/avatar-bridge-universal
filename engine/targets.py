"""Explicit game adapters, sharing one local avatar library."""
import hashlib
import json
from pathlib import Path
import shutil
import bridge as b

TARGETS = {'cyberpunk': 'Cyberpunk 2077', 'gmod': "Garry's Mod", 'portable': 'GLB universale (export)'}


def inspect_target(target, path):
    if target == 'cyberpunk': return b.inspect_game(path)
    if target == 'gmod':
        import gmod
        return gmod.inspect_game(path)
    if target == 'portable':
        return {'runtime': {'can_install':True, 'summary':'Export GLB con texture e scheletro. Ogni gioco richiede il proprio adattatore.'}}
    raise ValueError('Gioco non supportato: '+target)


def execute(target, command, job, game):
    if target == 'cyberpunk':
        import cp77
        return cp77.uninstall(game) if command=='uninstall' else getattr(cp77,command)(job,game)
    if target == 'gmod':
        import gmod
        return gmod.uninstall(game,job) if command=='uninstall' else getattr(gmod,command)(job,game)
    if target == 'portable' and command in {'build','install'}:
        root = Path(job).resolve()
        if not root.is_relative_to((b.DATA/'jobs').resolve()): raise ValueError('Lavoro fuori dalla libreria')
        report = json.loads((root/'report.json').read_text(encoding='utf-8'))
        source = Path(report['output']).resolve()
        if not source.is_relative_to(root): raise ValueError('Modello fuori dalla libreria')
        export = root/'portable'; export.mkdir(exist_ok=True)
        shutil.copy2(source,export/'avatar.glb')
        info = {'name':report.get('name','Avatar'),'category':report.get('category','Avatar'),
                'format':'glTF 2.0 binary','sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
                'note':'Risorsa sorgente. Serve un adattatore per il gioco di destinazione.'}
        (export/'avatar.json').write_text(json.dumps(info,indent=2),encoding='utf-8')
        return {'output':str(export)}
    raise ValueError('Operazione non disponibile per '+target)
