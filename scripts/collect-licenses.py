"""Collect dependency notices alongside binaries; never copy private build state."""
import json,pathlib,shutil,subprocess,sys
dest=pathlib.Path(sys.argv[1]);dest.mkdir(parents=True,exist_ok=True)
for source in sys.argv[2:]:
    repo=pathlib.Path(source)
    raw=subprocess.check_output(['go','list','-m','-json','all'],cwd=repo,text=True)
    (dest/(source+'-modules.json')).write_text(raw,encoding='utf-8')
    decoder=json.JSONDecoder();rest=raw
    while rest.strip():
        module,end=decoder.raw_decode(rest.lstrip());rest=rest.lstrip()[end:]
        location=module.get('Replace',module).get('Dir')
        if not location:continue
        directory=pathlib.Path(location)
        for notice in directory.iterdir():
            if notice.is_file() and notice.name.lower().startswith(('license','copying','notice','copyright')):
                target=dest/'go'/module['Path']/notice.name
                target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(notice,target)

