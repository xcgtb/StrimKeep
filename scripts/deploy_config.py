"""Render literal Compose in the same predictable order as the install example."""
import argparse
import json
import os
import re
from pathlib import Path

VERSION=re.search(r"__version__\s*=\s*'([^']+)'",(Path(__file__).resolve().parents[1]/'backend/strimkeep/__init__.py').read_text(encoding='utf-8')).group(1)


def scalar(value, preserve_interpolation=False):
    if not isinstance(value,str):return json.dumps(value,ensure_ascii=False)
    if not preserve_interpolation:
        value=value.replace('$','$$')
    ambiguous=re.fullmatch(r'(?:null|true|false|yes|no|on|off|~|[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?|\d{4}-\d\d-\d\d)',value,re.I)
    if value and not ambiguous and re.fullmatch(r"[\w./:=+@%,-]+",value,re.UNICODE) and not value.startswith(('-','@','%',',','=')):
        return value
    return json.dumps(value,ensure_ascii=False)


def yaml(value,indent=0,preserve_interpolation=False):
    lines=[];pad=' '*indent
    def render(item):
        return scalar(item, preserve_interpolation)

    if isinstance(value,dict):
        for key,item in value.items():
            if isinstance(item,(dict,list)) and item:
                lines.append(pad+render(str(key))+':');lines.extend(yaml(item,indent+2,preserve_interpolation))
            else:lines.append(pad+render(str(key))+': '+render(item))
    else:
        for item in value:
            if isinstance(item,(dict,list)) and item:
                lines.append(pad+'-');lines.extend(yaml(item,indent+2,preserve_interpolation))
            else:lines.append(pad+'- '+render(item))
    return lines


def ordered(value,keys):
    return {**{k:value[k] for k in keys if k in value},**{k:v for k,v in value.items() if k not in keys}}


ENV_SAFE = re.compile(r'^[A-Za-z0-9!@%^_+=:,./-]+$')
# scalar() 会把 $ 转义成 $$（防止 compose 误插值），所以变量引用先用纯字母占位，
# 渲染完成后再替换成 ${ADMIN_PASSWORD}。
ENV_PLACEHOLDER = 'SKPLACEHOLDERMAINPASSWORD'


def env_reference(name, value, env_path):
    """把字面口令搬进项目 .env，compose 里只留 ${NAME} 引用。

    `docker compose config` 会把 ${ADMIN_PASSWORD} 解析成明文，旧流程再把它写回
    compose，等于每次更新都往可能被提交/备份的文件里放一次口令。这里先写 .env
    （gitignore，权限 600），compose 只保留变量名。

    写不进 .env、或口令里带单引号时退回字面值：更新流程不能被这件事卡死。
    """
    if not env_path or "'" in str(value):
        return value
    try:
        path = Path(env_path)
        lines = path.read_text(encoding='utf-8').splitlines() if path.is_file() else []
        # 单引号在 .env 里是字面量（不展开 $），比双引号安全；含单引号时上面已退回字面值。
        rendered = value if ENV_SAFE.match(value) else "'%s'" % value
        entry = '%s=%s' % (name, rendered)
        prefix = name + '='
        result, replaced = [], False
        for line in lines:
            if line.startswith(prefix):
                if not replaced:
                    result.append(entry)
                    replaced = True
                continue
            result.append(line)
        if not replaced:
            result.append(entry)
        path.write_text('\n'.join(result) + '\n', encoding='utf-8')
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return '${%s}' % name
    except (OSError, UnicodeError):
        return value


def convert(model, env_path=None, *, preserve_interpolation=False):
    service=model['services']['strimkeep']
    service['image']='strimkeep:'+VERSION
    build=service.get('build',{})
    service['build']='.' if not isinstance(build,dict) or set(build)<={'context','dockerfile'} and build.get('dockerfile','Dockerfile')=='Dockerfile' else {**build,'context':'.'}
    for k in ('command','entrypoint'):
        if service.get(k) is None:service.pop(k,None)
    # Restore omitted defaults from 0.6.4 onward; preserve explicit custom settings.
    if service.get('init') is None:service['init']=True
    if service.get('logging') is None:
        service['logging']={'driver':'json-file','options':{'max-size':'10m','max-file':'3'}}
    env=service.get('environment',{})
    reference=None
    if isinstance(env,dict):
        if str(env.get('SESSION_COOKIE_SECURE','')).lower() in ('','false'):env.pop('SESSION_COOKIE_SECURE',None)
        if not env.get('ALLOWED_ORIGINS'):env.pop('ALLOWED_ORIGINS',None)
        # 口令永远只做变量引用，不把明文写回 compose（先占位，渲染后再还原成 ${...}）。
        password = str(env.get('ADMIN_PASSWORD') or '')
        if password and (not preserve_interpolation or '$' not in password.replace('$$', '')):
            if preserve_interpolation:
                password = password.replace('$$', '$')
            reference=env_reference('ADMIN_PASSWORD',password,env_path)
            if reference.startswith('${'):env['ADMIN_PASSWORD']=ENV_PLACEHOLDER
        if env:
            env=ordered(env,('TZ','ADMIN_USERNAME','ADMIN_PASSWORD'))
            service['environment']=[k if v is None else k+'='+str(v) for k,v in env.items()]
        else:
            service.pop('environment',None)
    for n, mount in enumerate(service.get('volumes', [])):
        if not isinstance(mount, dict):
            continue
        source = mount.get('source', '')
        mount_type = mount.get('type')
        simple_bind = (
            mount_type == 'bind'
            and set(mount) <= {'type', 'source', 'target', 'read_only', 'bind'}
            and set(mount.get('bind', {})) <= {'create_host_path'}
            and mount.get('bind', {}).get('create_host_path', True) is not False
            and ':' not in source
        )
        # A short mount with a variable source is classified before interpolation.
        # Keep short syntax so Compose can infer bind vs. named volume after expansion.
        variable_volume = (
            preserve_interpolation and mount_type == 'volume' and source.startswith('$')
            and set(mount) <= {'type', 'source', 'target', 'read_only', 'volume'}
            and not mount.get('volume')
        )
        if source and (simple_bind or variable_volume):
            service['volumes'][n] = source + ':' + mount['target'] + (':ro' if mount.get('read_only') else '')
    model['services']['strimkeep']=ordered(service,('image','build','container_name','network_mode','env_file','environment','volumes','restart','init','logging'))
    model.pop('version',None)
    model=ordered(model,('name','services'))
    # Keep blank lines between project name and services, as in the reference.
    text='\n\n'.join('\n'.join(yaml({k:v},preserve_interpolation=preserve_interpolation)) for k,v in model.items())+'\n'
    if reference and reference.startswith('${'):
        text=text.replace('ADMIN_PASSWORD='+ENV_PLACEHOLDER,'ADMIN_PASSWORD='+reference)
    return text


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('model', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('env_path', nargs='?')
    parser.add_argument('--preserve-interpolation', action='store_true')
    args = parser.parse_args()
    output = args.output
    model = json.loads(args.model.read_text(encoding='utf-8'))
    output.write_text(convert(model, args.env_path, preserve_interpolation=args.preserve_interpolation), encoding='utf-8')
    os.chmod(output,0o600)
