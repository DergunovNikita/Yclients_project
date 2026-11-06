import { jpegToPdf, snapshotFileName, snapshotPixelRatio } from './snapshotFile.js';
import { t } from './i18n.js';

const SHEET_PADDING = 16;
const JPEG_QUALITY = 0.92;
// Controls and placeholders are not part of a table's picture; `[hidden]` is gone from the clone because the
// rasteriser would otherwise draw what the page keeps out of sight.
const STRIPPED = 'button, select, input, .view-loader, [hidden]';

let busy = false;
// A session transition bumps it: whatever was being rasterised before it is dropped, the dialog stays shut.
let generation = 0;
// What the open dialog holds: the canvas (the PDF is cut from it), the files and their object URLs.
let shown = null;
// The button to give focus back to: it is disabled while the picture is drawn, and a disabled button drops focus,
// so the dialog's own focus restoration would land on <body>.
let opener = null;

function dialogParts() {
  const dialog = document.getElementById('table-snapshot-dialog');
  if (!dialog) return null;
  return {
    dialog,
    hint: dialog.querySelector('#table-snapshot-hint'),
    stage: dialog.querySelector('#table-snapshot-stage'),
    image: dialog.querySelector('#table-snapshot-image'),
    caption: dialog.querySelector('#table-snapshot-caption'),
    message: dialog.querySelector('#table-snapshot-message'),
    share: dialog.querySelector('#table-snapshot-share'),
    png: dialog.querySelector('#table-snapshot-png'),
    pdf: dialog.querySelector('#table-snapshot-pdf'),
    close: dialog.querySelector('#table-snapshot-close'),
  };
}

function setMessage(parts, text) {
  parts.message.textContent = text;
  parts.message.hidden = !text;
}

function releaseShown() {
  if (!shown) return;
  shown.urls.forEach((url) => URL.revokeObjectURL(url));
  // A canvas keeps its pixels until it is resized; up to 16M of them are not worth waiting for the collector.
  shown.canvas.width = 0;
  shown.canvas.height = 0;
  shown = null;
  const parts = dialogParts();
  if (parts) parts.image.removeAttribute('src');
}

function returnFocus() {
  if (opener?.isConnected && !opener.hidden && !opener.disabled) opener.focus();
  opener = null;
}

function closeDialog() {
  const parts = dialogParts();
  if (parts?.dialog.open) parts.dialog.close();
  releaseShown();
}

function urlOf(file) {
  let url = shown.urls.get(file);
  if (!url) {
    url = URL.createObjectURL(file);
    shown.urls.set(file, url);
  }
  return url;
}

function canvasBlob(canvas, type, quality) {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('canvas.toBlob returned no data'))), type, quality);
  });
}

function canShareFile(file) {
  try {
    return Boolean(navigator.canShare?.({ files: [file] }));
  } catch {
    return false;
  }
}

// Not async before `navigator.share`: iOS Safari wants the call inside the click's user activation, so the file has
// to be ready by now (the PDF is cut when the dialog opens, not on the click).
function shareFile(file) {
  const current = shown;
  navigator.share({ files: [file] }).catch((error) => {
    if (error?.name === 'AbortError' || shown !== current) return;
    setMessage(dialogParts(), t('snapshot.shareFailed'));
  });
}

function downloadFile(file) {
  const link = document.createElement('a');
  link.href = urlOf(file);
  link.download = file.name;
  document.body.append(link);
  link.click();
  link.remove();
}

function openDialog(parts) {
  if (!parts.dialog.open) parts.dialog.showModal();
}

function showMessageOnly(text) {
  const parts = dialogParts();
  if (!parts) return;
  releaseShown();
  parts.hint.hidden = true;
  parts.caption.hidden = true;
  parts.stage.hidden = true;
  [parts.share, parts.png, parts.pdf].forEach((button) => { button.hidden = true; });
  setMessage(parts, text);
  openDialog(parts);
  parts.close.focus();
}

function showSnapshot({ canvas, pngBlob, title, fileParts }) {
  const parts = dialogParts();
  releaseShown();
  const current = {
    canvas,
    urls: new Map(),
    pngFile: new File([pngBlob], snapshotFileName(fileParts, 'png'), { type: 'image/png' }),
    pdfFile: null,
  };
  shown = current;
  parts.image.src = urlOf(current.pngFile);
  parts.caption.textContent = title;
  parts.caption.hidden = false;
  parts.hint.hidden = false;
  parts.stage.hidden = false;
  parts.stage.scrollTop = 0;
  parts.png.hidden = false;
  parts.pdf.hidden = false;
  parts.pdf.disabled = true;
  parts.share.hidden = !canShareFile(current.pngFile);
  setMessage(parts, '');
  openDialog(parts);
  parts.close.focus();

  canvasBlob(canvas, 'image/jpeg', JPEG_QUALITY)
    .then((blob) => blob.arrayBuffer())
    .then((buffer) => {
      if (shown !== current) return;
      const pdf = jpegToPdf(new Uint8Array(buffer), canvas.width, canvas.height);
      current.pdfFile = new File([pdf], snapshotFileName(fileParts, 'pdf'), { type: 'application/pdf' });
      parts.pdf.disabled = false;
    })
    .catch(() => {
      if (shown === current) setMessage(parts, t('snapshot.pdfFailed'));
    });
}

function cloneForSnapshot(node) {
  const clone = node.cloneNode(true);
  clone.querySelectorAll(STRIPPED).forEach((element) => element.remove());
  // The clone sits in the same document as the page: a duplicated id would hijack getElementById, and a jump target
  // would put a second block into the floating navigation.
  [clone, ...clone.querySelectorAll('[id], [data-jump-target]')].forEach((element) => {
    element.removeAttribute('id');
    element.removeAttribute('data-jump-target');
    element.removeAttribute('data-jump-label');
  });
  return clone;
}

// The sheet is as wide as the live content (or its widest table), so the tables lay out exactly as on screen,
// including the colgroup widths alignPlanTableColumns put on them. The host is off-screen and the library draws the
// inner sheet, which is in normal flow: it copies computed styles, and a `fixed; left: -100000px` root would carry
// its own offset into the picture.
function buildSheet(nodes, title, meta) {
  const live = nodes.filter(Boolean);
  const contentWidth = Math.ceil(Math.max(0, ...live.flatMap((node) => [
    node.clientWidth,
    ...[...node.querySelectorAll('table')].map((table) => table.offsetWidth),
  ])));
  if (!contentWidth) return null;

  const sheet = document.createElement('div');
  sheet.className = 'table-snapshot-sheet';
  sheet.style.width = `${contentWidth + 2 * SHEET_PADDING}px`;
  sheet.style.padding = `${SHEET_PADDING}px`;
  const caption = document.createElement('header');
  caption.className = 'table-snapshot-sheet__caption';
  const heading = document.createElement('h2');
  heading.textContent = title;
  caption.append(heading);
  if (meta) {
    const line = document.createElement('p');
    line.textContent = meta;
    caption.append(line);
  }
  sheet.append(caption, ...live.map(cloneForSnapshot));

  const host = document.createElement('div');
  host.className = 'table-snapshot-host';
  host.setAttribute('aria-hidden', 'true');
  host.append(sheet);
  document.body.append(host);
  return { host, sheet };
}

function setTriggerBusy(trigger, isBusy) {
  if (!trigger) return;
  trigger.disabled = isBusy;
  if (isBusy) trigger.setAttribute('aria-busy', 'true');
  else trigger.removeAttribute('aria-busy');
}

/**
 * Picture of live table markup in a dialog, with share / PNG / PDF actions.
 *
 * The nodes are cloned before the first await, so a view that re-renders while the picture is being drawn
 * changes nothing in it.
 *
 * @param {object} options
 * @param {Element[]} options.nodes Live elements to draw, in order.
 * @param {string} options.title Caption heading.
 * @param {string} [options.meta] Caption line under the heading.
 * @param {string[]} [options.fileParts] Pieces of the downloaded file name.
 * @param {HTMLElement} [options.trigger] Button that started it: busy while the picture is drawn.
 */
export async function openTableSnapshot({ nodes, title, meta = '', fileParts = [], trigger = null }) {
  if (busy || !dialogParts()) return;
  busy = true;
  opener = trigger;
  const run = generation;
  setTriggerBusy(trigger, true);
  let built = null;
  let failure = 'snapshot.failed';
  try {
    built = buildSheet(nodes, title, meta);
    if (!built) throw new Error('nothing to draw');
    failure = 'snapshot.moduleFailed';
    // Its own chunk: the library is only paid for by whoever takes a snapshot. A deploy that removed the chunk
    // from under an open tab fails here.
    const { toCanvas } = await import('html-to-image');
    failure = 'snapshot.failed';
    const { offsetWidth: width, offsetHeight: height } = built.sheet;
    const canvas = await toCanvas(built.sheet, {
      backgroundColor: '#ffffff',
      pixelRatio: snapshotPixelRatio(width, height),
      skipFonts: true,
      cacheBust: false,
    });
    built.host.remove();
    built = null;
    const pngBlob = await canvasBlob(canvas, 'image/png');
    if (run !== generation) return;
    showSnapshot({ canvas, pngBlob, title, fileParts: fileParts.length ? fileParts : [title] });
  } catch (error) {
    console.warn('table snapshot failed', error);
    if (run === generation) showMessageOnly(t(failure));
  } finally {
    built?.host.remove();
    busy = false;
    setTriggerBusy(trigger, false);
  }
}

function bindDialog() {
  const parts = dialogParts();
  if (!parts) return;
  parts.dialog.addEventListener('close', () => {
    releaseShown();
    returnFocus();
  });
  parts.dialog.addEventListener('click', (event) => {
    if (event.target === parts.dialog) parts.dialog.close();
  });
  parts.close.addEventListener('click', () => parts.dialog.close());
  parts.share.addEventListener('click', () => {
    if (shown) shareFile(shown.pngFile);
  });
  parts.png.addEventListener('click', () => {
    if (shown) downloadFile(shown.pngFile);
  });
  parts.pdf.addEventListener('click', () => {
    const file = shown?.pdfFile;
    if (!file) return;
    if (canShareFile(file)) shareFile(file);
    else downloadFile(file);
  });
}

// Sign-out, an ended session or another person signing in: the picture is the person's data and goes with them.
function bindSessionTransition() {
  window.addEventListener('portal:session-transition', () => {
    generation += 1;
    closeDialog();
  });
}

if (typeof document !== 'undefined') {
  bindDialog();
  bindSessionTransition();
}
