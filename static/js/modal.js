
/* StrimKeep - UI Modal v1 */
(function(){
  var overlay = null;

  function ensureOverlay(){
    if (overlay) return overlay;
    overlay = document.createElement('div');
    overlay.className = 'ui-modal-bg';
    overlay.innerHTML = '<div class="ui-modal-box" id="uiModalBox"></div>';
    overlay.addEventListener('click', function(e){
      if (e.target === overlay) {
        var box = document.getElementById('uiModalBox');
        if (box && box.dataset.dismissable === '1') overlay.classList.remove('show');
      }
    });
    document.body.appendChild(overlay);
    return overlay;
  }

  function show(type, title, message, options){
    options = options || {};
    ensureOverlay();
    var box = document.getElementById('uiModalBox');
    box.dataset.dismissable = (type === 'alert') ? '1' : '0';
    box.className = 'ui-modal-box ui-modal-' + type;

    var iconName = options.icon || (type === 'confirm' ? 'alert' : type === 'success' ? 'check' : type === 'error' ? 'x' : 'info');
    var iconCls = type === 'success' ? ' ui-modal-icon-ok' : type === 'error' ? ' ui-modal-icon-err' : '';

    var okText = options.okText || '\u77e5\u9053\u4e86';
    var cancelText = options.cancelText || '\u53d6\u6d88';
    var confirmText = options.confirmText || '\u786e\u8ba4';

    var buttonsHtml;
    if (type === 'confirm') {
      buttonsHtml = '<div class="ui-modal-actions">'
        + '<button class="btn gray" id="uiModalCancel">' + cancelText + '</button>'
        + '<button class="btn ' + (options.danger ? 'red' : '') + '" id="uiModalOk">' + confirmText + '</button>'
        + '</div>';
    } else {
      buttonsHtml = '<div class="ui-modal-actions">'
        + '<button class="btn" id="uiModalOk">' + okText + '</button>'
        + '</div>';
    }

    var msgHtml;
    if (Array.isArray(message)) {
      msgHtml = message.map(function(line){
        return '<div class="ui-modal-line">' + String(line).replace(/\n/g, '<br>') + '</div>';
      }).join('');
    } else {
      msgHtml = '<div class="ui-modal-line">' + String(message).replace(/\n/g, '<br>') + '</div>';
    }

    box.innerHTML = '<div class="ui-modal-icon' + iconCls + '">' + icon(iconName) + '</div>'
      + '<h3 class="ui-modal-title">' + (title || '') + '</h3>'
      + '<div class="ui-modal-body">' + msgHtml + '</div>'
      + buttonsHtml;

    overlay.classList.add('show');

    return new Promise(function(resolve){
      var ok = document.getElementById('uiModalOk');
      var cancel = document.getElementById('uiModalCancel');
      function finish(v){ overlay.classList.remove('show'); resolve(v); }
      if (ok) ok.onclick = function(){ finish(true); };
      if (cancel) cancel.onclick = function(){ finish(false); };
    });
  }

  window.showModalAlert = function(message, options){
    options = options || {};
    return show('alert', options.title || '\u63d0\u793a', message, options);
  };
  window.showModalConfirm = function(message, options){
    options = options || {};
    return show('confirm', options.title || '\u786e\u8ba4\u64cd\u4f5c', message, options);
  };
  window.showModalSuccess = function(message, options){
    options = options || {};
    return show('success', options.title || '\u64cd\u4f5c\u6210\u529f', message, options);
  };
  window.showModalError = function(message, options){
    options = options || {};
    return show('error', options.title || '\u64cd\u4f5c\u5931\u8d25', message, options);
  };
})();

