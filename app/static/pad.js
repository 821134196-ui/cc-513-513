// 手写签名板：输出 PNG dataURL 到隐藏输入框
function initPad(canvasId, hiddenId) {
  const canvas = document.getElementById(canvasId);
  const hidden = document.getElementById(hiddenId);
  const clearBtn = document.getElementById(canvasId + '-clear');
  const ctx = canvas.getContext('2d');
  let drawing = false, dirty = false;

  function resize() {
    const ratio = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width === Math.round(w * ratio)) return;
    const data = dirty ? canvas.toDataURL() : null;
    canvas.width = w * ratio; canvas.height = h * ratio;
    ctx.scale(ratio, ratio);
    ctx.lineWidth = 2; ctx.lineCap = 'round'; ctx.strokeStyle = '#333333';
    if (data) {
      const img = new Image();
      img.onload = () => ctx.drawImage(img, 0, 0, w, h);
      img.src = data;
    }
  }
  resize();
  window.addEventListener('resize', resize);

  function pos(e) {
    const r = canvas.getBoundingClientRect();
    const t = e.touches ? e.touches[0] : e;
    return [t.clientX - r.left, t.clientY - r.top];
  }
  function start(e) {
    e.preventDefault();
    drawing = true; dirty = true;
    const [x, y] = pos(e);
    ctx.beginPath(); ctx.moveTo(x, y);
  }
  function move(e) {
    if (!drawing) return;
    e.preventDefault();
    const [x, y] = pos(e);
    ctx.lineTo(x, y); ctx.stroke();
  }
  function end() {
    if (!drawing) return;
    drawing = false;
    hidden.value = canvas.toDataURL('image/png');
  }
  canvas.addEventListener('mousedown', start);
  canvas.addEventListener('mousemove', move);
  window.addEventListener('mouseup', end);
  canvas.addEventListener('touchstart', start, {passive: false});
  canvas.addEventListener('touchmove', move, {passive: false});
  canvas.addEventListener('touchend', end);
  clearBtn.addEventListener('click', () => {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    dirty = false; hidden.value = '';
  });
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('canvas.pad').forEach(c => {
    initPad(c.id, c.dataset.hidden);
  });
});
