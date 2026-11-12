import importlib.util
from pathlib import Path


def converter():
    spec=importlib.util.spec_from_file_location('deploy064',Path(__file__).resolve().parents[2]/'scripts/deploy_config.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def test_resolved_stock_config_has_reference_order_and_list_environment():
    model={'name':'strimkeep','services':{'strimkeep':{'build':{'context':'/old'},'command':None,'container_name':'strimkeep','entrypoint':None,'environment':{'ADMIN_PASSWORD':'old-password','ADMIN_USERNAME':'admin','TZ':'Asia/Shanghai'},'image':'strimkeep:0.6.3','init':True,'logging':{'driver':'json-file','options':{'max-file':'3','max-size':'10m'}},'network_mode':'host','restart':'unless-stopped','volumes':[{'type':'bind','source':'/NAS/测试/data','target':'/data','bind':{'create_host_path':True}}]}}}
    text=converter().convert(model)
    keys=['    image:','    build:','    container_name:','    network_mode:','    environment:','    volumes:','    restart:']
    assert [text.index(k) for k in keys]==sorted(text.index(k) for k in keys)
    assert 'name: strimkeep\n\nservices:' in text
    assert '- TZ=Asia/Shanghai\n      - ADMIN_USERNAME=admin\n      - ADMIN_PASSWORD=old-password' in text
    assert 'command:' not in text and 'entrypoint:' not in text and 'null' not in text
    assert 'driver: json-file' in text and 'init: true' in text
    assert 'max-size: 10m' in text and 'max-file: \"3\"' in text
    assert '/NAS/测试/data:/data' in text and '${' not in text


def test_custom_settings_and_explicit_empty_command_are_preserved():
    model={'name':'custom','services':{'strimkeep':{'command':[],'entrypoint':['custom-entry'],'environment':{'TZ':'Asia/Shanghai','CUSTOM':'a$with: colon #comment'},'logging':{'driver':'local'},'init':True,'volumes':[{'type':'bind','source':'/existing','target':'/data','bind':{'create_host_path':False}}],'dns':['1.1.1.1']}}}
    text=converter().convert(model)
    assert 'command: []' in text and 'custom-entry' in text
    assert 'driver: local' in text and 'init: true' in text and 'create_host_path: false' in text
    assert 'a$$with: colon #comment' in text and 'dns:' in text


def test_update_restores_missing_defaults_but_keeps_explicit_custom_settings():
    module=converter()
    missing={'services':{'strimkeep':{'image':'strimkeep:0.6.4'}}}
    restored=module.convert(missing)
    assert 'init: true' in restored and 'driver: json-file' in restored
    assert 'max-size: 10m' in restored and 'max-file: "3"' in restored
    custom={'services':{'strimkeep':{'init':False,'logging':{'driver':'json-file','options':{'max-size':'5m','max-file':'8'}}}}}
    preserved=module.convert(custom)
    assert 'init: false' in preserved and 'max-size: 5m' in preserved
    assert 'max-file: "8"' in preserved
