import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { ApiError, StaleIdentityError, request, cookieLock, invalidateIdentity, onInvalidIdentity, identityGeneration } from './api.js';
import { fields, makePreferences, terminalTask, retryable, originalExtension, validateFileMetadata } from './domain.js';

export function useStudio() {
  const mode = import.meta.env.VITE_FLOW_MODE === 'chatflow' ? 'chatflow' : 'standalone';
  const identity = ref(null), busy = ref(''), error = ref(null), notice = ref('');
  const view = ref('create'), category = ref(''), primary = ref(null), auxiliaries = ref([]);
  const reference = ref(null), recommendation = ref(null), session = ref(null), task = ref(null);
  const history = ref([]), cursor = ref(null), historyLoaded = ref(false), historyFilter = ref('all');
  const modes = reactive(Object.fromEntries(fields.map(f => [f.key, 'A'])));
  const custom = reactive(Object.fromEntries(fields.map(f => [f.key, ''])));
  const platform = ref(''), imageTypes = ref([]), pending = ref(null), downloadBusy = ref(false);
  const previews = reactive({}), previewErrors = reactive({});
  const modalImage = ref(null), resetting = ref(false);
  const tasksAvailable = ref(true), sessionAvailable = ref(true);
  const channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('visual-studio:identity') : null;
  let timer, identityTimer, pollRunning = false, closed = false, selectionInitialized = false;
  const loadingPreviews = new Set();
  const options = computed(() => recommendation.value?.options || recommendation.value?.public_options);
  const canSelect = computed(() => !!options.value && !task.value && !pending.value &&
    (mode === 'standalone' ? recommendation.value?.status === 'ready' : session.value?.selection_ready === true));
  const filteredHistory = computed(() => history.value.filter(t => historyFilter.value === 'all' ||
    (historyFilter.value === 'active' ? !terminalTask(t.status) : terminalTask(t.status))));
  const failedItems = computed(() => task.value?.items.filter(retryable) || []);
  const step = computed(() => task.value ? 3 : options.value ? 2 : 1);
  const canNewRecommendation = computed(() => !pending.value && !task.value &&
    (mode === 'chatflow' ? !session.value || ['failed', 'expired', 'cancelled'].includes(session.value.status) :
      !recommendation.value || ['failed', 'expired'].includes(recommendation.value.status)));
  function revokePreviews() {
    for (const key in previews) { URL.revokeObjectURL(previews[key]); delete previews[key]; }
    for (const key in previewErrors) delete previewErrors[key];
    modalImage.value = null;
  }
  function clearFiles() {
    if (primary.value) URL.revokeObjectURL(primary.value.url);
    auxiliaries.value.forEach(f => URL.revokeObjectURL(f.url));
    primary.value = null; auxiliaries.value = [];
  }
  function clearFlow() {
    clearFiles(); revokePreviews(); category.value = ''; reference.value = null;
    recommendation.value = null; session.value = null; task.value = null; pending.value = null;
    imageTypes.value = []; platform.value = ''; selectionInitialized = false;
    fields.forEach(f => { modes[f.key] = 'A'; custom[f.key] = ''; });
    sessionStorage.removeItem('visual-studio:flow');
  }
  const unsubscribe = onInvalidIdentity(() => {
    identity.value = null; clearFlow(); history.value = []; cursor.value = null; historyLoaded.value = false;
    notice.value = '会话已变更或失效，旧身份的数据已清除。请重新连接访客会话。';
  });
  function showError(e) {
    if (e instanceof StaleIdentityError) return;
    error.value = { message: e.message, code: e.code, requestId: e.requestId };
  }
  async function action(label, fn) {
    if (busy.value) return;
    busy.value = label; error.value = null; notice.value = '';
    try { await fn(); } catch (e) { showError(e); } finally { busy.value = ''; }
  }
  function persist() {
    if (!identity.value) return;
    try {
      // Only public identifiers and an unfinished request; no cookies, blobs or private prompts.
      sessionStorage.setItem('visual-studio:flow', JSON.stringify({
        owner: identity.value.user_id, mode, category: category.value,
        referenceId: reference.value?.reference_set_id, recommendationId: recommendation.value?.recommendation_id,
        sessionId: session.value?.business_session_id, taskId: task.value?.task_id,
        modes, custom, platform: platform.value, imageTypes: imageTypes.value, pending: pending.value,
      }));
    } catch { /* Browser storage may be disabled; in-memory workflow still works. */ }
  }
  watch([reference, recommendation, session, task, modes, custom, platform, imageTypes, pending], persist, { deep: true });
  function broadcast() { channel?.postMessage({ userId: identity.value?.user_id }); }
  channel && (channel.onmessage = async event => {
    if (event.data.userId !== identity.value?.user_id) {
      invalidateIdentity();
      try {
        identity.value = await request('/auth/me');
        notice.value = '另一标签页更新了访客身份，已切换到当前身份。';
        await checkCapabilities();
      } catch (e) { showError(e); }
    }
  });
  async function checkCapabilities() {
    try { await request('/tasks?limit=1'); tasksAvailable.value = true; }
    catch (e) { if (e.status === 404) tasksAvailable.value = false; else throw e; }
    if (mode === 'chatflow') {
      try { await request('/business-sessions/00000000-0000-0000-0000-000000000000'); }
      catch (e) {
        // Both absent route and unknown resource can be 404. No mutation probe.
        if (e.status !== 404) throw e;
      }
    }
    await Promise.all([request('/quota'), request('/billing/balance')]);
  }
  async function connect() {
    await action('连接访客会话', async () => {
      identity.value = await cookieLock(() => request('/auth/guest', { method: 'POST' }));
      broadcast(); await checkCapabilities();
      let saved;
      try { saved = JSON.parse(sessionStorage.getItem('visual-studio:flow')); } catch { /* ignored */ }
      if (saved?.owner === identity.value.user_id && saved.mode === mode) {
        category.value = saved.category || '';
        if (saved.referenceId) reference.value = await request(`/reference-sets/${saved.referenceId}`);
        Object.assign(modes, saved.modes); Object.assign(custom, saved.custom);
        platform.value = saved.platform || ''; imageTypes.value = saved.imageTypes || [];
        selectionInitialized = !!saved.platform;
        pending.value = saved.pending || null;
        if (saved.sessionId) session.value = await request(`/business-sessions/${saved.sessionId}`);
        else if (saved.recommendationId) recommendation.value = await request(`/recommendations/${saved.recommendationId}`);
        if (saved.taskId) task.value = await request(`/tasks/${saved.taskId}`);
        if (session.value?.recommendation) recommendation.value = session.value.recommendation;
        initializeSelection();
        await loadPreviews();
      } else sessionStorage.removeItem('visual-studio:flow');
    });
  }
  async function refresh() {
    if (!identity.value) throw new Error('请先连接访客会话');
    if (identity.value.identity_kind === 'guest') await cookieLock(() => request('/auth/guest/refresh', { method: 'POST' }));
    // Authenticate even for a member; only the server determines identity validity.
    const current = await request('/auth/me');
    if (current.user_id !== identity.value?.user_id) { invalidateIdentity(); throw new StaleIdentityError(); }
  }
  async function chooseFiles(event, slot) {
    const selected = Array.from(event.target.files || []); event.target.value = '';
    if (!selected.length) return;
    await action('检查图片', async () => {
      const files = slot === 'primary' ? [selected[0], ...auxiliaries.value.map(f => f.file)] :
        [primary.value?.file, ...auxiliaries.value.map(f => f.file), ...selected].filter(Boolean);
      validateFileMetadata(files);
      if (slot !== 'primary' && auxiliaries.value.length + selected.length > 3) throw new Error('最多上传 3 张辅图');
      for (const file of selected) {
        const bitmap = await createImageBitmap(file).catch(() => { throw new Error(`无法读取图片：${file.name}`); });
        const pixels = bitmap.width * bitmap.height; bitmap.close();
        if (pixels > 25000000) throw new Error('图片不能超过 2500 万像素');
      }
      if (slot === 'primary') {
        if (primary.value) URL.revokeObjectURL(primary.value.url);
        primary.value = { file: selected[0], url: URL.createObjectURL(selected[0]) };
      } else auxiliaries.value.push(...selected.map(file => ({ file, url: URL.createObjectURL(file) })));
    });
  }
  function removeFile(index) {
    const file = auxiliaries.value.splice(index, 1)[0]; if (file) URL.revokeObjectURL(file.url);
  }
  function initializeSelection() {
    if (!options.value || selectionInitialized) return;
    platform.value = options.value.platform[0];
    imageTypes.value = recommendation.value.image_types.slice(0, 1);
    selectionInitialized = true;
  }
  async function sendPending() {
    const op = pending.value;
    if (!op) return;
    try {
      const result = await request(op.path, { method: 'POST', body: op.body, key: op.key });
      if (op.kind === 'start') session.value = result;
      if (op.kind === 'submit') { task.value = { ...result, items: [], counts: { planned: result.planned_count, succeeded: 0, failed: 0, pending: result.planned_count, unresolved: 0 } }; }
      if (op.kind === 'retry') notice.value = '失败图片已提交重试。';
      pending.value = null;
      if (task.value) { task.value = await request(`/tasks/${task.value.task_id}`); await loadPreviews(); }
      if (session.value?.business_session_id) await pollSession();
      persist();
    } catch (e) {
      if (e instanceof StaleIdentityError || e.status === 401) throw e;
      // Timeout/5xx can follow a commit. Keep EXACT body/key for explicit replay.
      if (e.status >= 400 && e.status < 500) pending.value = null;
      if (e.status === 404 && op.kind === 'start') sessionAvailable.value = false;
      throw e;
    }
  }
  async function startRecommendation() {
    if (!reference.value) throw new Error('请先上传参考图片');
    if (mode === 'chatflow') {
      pending.value = { kind: 'start', path: '/business-sessions', body: { reference_set_id: reference.value.reference_set_id }, key: crypto.randomUUID() };
      persist(); await sendPending();
    } else {
      // This endpoint has no idempotency contract: never silently resubmit.
      const accepted = await request('/recommendations', { method: 'POST', body: { reference_set_id: reference.value.reference_set_id } });
      recommendation.value = accepted; persist();
    }
  }
  async function upload() {
    await action('上传并获取推荐', async () => {
      if (!category.value.trim() || category.value.trim().length > 100) throw new Error('商品类别需为 1–100 字');
      if (!primary.value) throw new Error('请先选择商品主图');
      validateFileMetadata([primary.value.file, ...auxiliaries.value.map(f => f.file)]);
      await refresh();
      const form = new FormData();
      form.append('product_category', category.value.trim());
      form.append('primary_product_image', primary.value.file);
      auxiliaries.value.forEach(f => form.append('supplementary_product_images', f.file));
      reference.value = await request('/reference-sets', { method: 'POST', body: form, timeout: 120000 });
      persist(); await startRecommendation();
    });
  }
  async function newRecommendation() {
    await action('获取新的推荐', async () => {
      await refresh(); recommendation.value = null; session.value = null; selectionInitialized = false;
      await startRecommendation();
    });
  }
  async function submit() {
    if (!canSelect.value) return;
    await action('提交生成任务', async () => {
      if (!imageTypes.value.length || imageTypes.value.some(t => !recommendation.value.image_types.includes(t))) throw new Error('请选择 1–8 种图片类型');
      const body = {
        recommendation_id: recommendation.value.recommendation_id,
        recommendation_revision: recommendation.value.revision || recommendation.value.recommendation_revision,
        preferences: makePreferences(modes, custom, platform.value, options.value), image_types: [...imageTypes.value],
      };
      await refresh();
      pending.value = {
        kind: 'submit', key: crypto.randomUUID(),
        path: mode === 'chatflow' ? `/business-sessions/${session.value.business_session_id}/selection` : '/tasks',
        body: mode === 'chatflow' ? body : { schema_version: 2, reference_set_id: reference.value.reference_set_id, ...body },
      };
      persist(); await sendPending();
    });
  }
  async function replayPending() { await action('确认原操作', async () => { await refresh(); await sendPending(); }); }
  async function pollSession() {
    const id = session.value?.business_session_id;
    if (!id) return;
    const data = await request(`/business-sessions/${id}`);
    if (id !== session.value?.business_session_id) return;
    session.value = data;
    if (data.recommendation) { recommendation.value = data.recommendation; initializeSelection(); }
    if (data.task_id && task.value?.task_id !== data.task_id) task.value = await request(`/tasks/${data.task_id}`);
  }
  async function loadPreviews() {
    const current = task.value;
    if (!current) return;
    for (const item of current.items) {
      if (!item.preview_ready || !item.image_id || previews[item.image_id] || loadingPreviews.has(item.image_id) || previewErrors[item.image_id]) continue;
      loadingPreviews.add(item.image_id);
      try {
        const result = await request(`/tasks/${current.task_id}/images/${item.image_id}/preview`, { binary: true });
        if (current.task_id !== task.value?.task_id) continue;
        if (!result.blob.type.startsWith('image/')) throw new Error('图片预览类型无效');
        previews[item.image_id] = URL.createObjectURL(result.blob);
      } catch (e) { if (!(e instanceof StaleIdentityError)) previewErrors[item.image_id] = e.message; }
      finally { loadingPreviews.delete(item.image_id); }
    }
  }
  async function reloadPreview(item) { delete previewErrors[item.image_id]; await loadPreviews(); }
  async function poll() {
    if (!identity.value || pollRunning || document.hidden || busy.value) return;
    pollRunning = true;
    try {
      if (session.value && !terminalTask(task.value?.status) && !['failed', 'expired', 'cancelled'].includes(session.value.status)) await pollSession();
      if (mode === 'standalone' && recommendation.value?.status === 'analyzing') {
        const id = recommendation.value.recommendation_id;
        const data = await request(`/recommendations/${id}`);
        if (id === recommendation.value?.recommendation_id) { recommendation.value = data; initializeSelection(); }
      }
      if (task.value && !terminalTask(task.value.status)) {
        const id = task.value.task_id;
        const data = await request(`/tasks/${id}`);
        if (id === task.value?.task_id) task.value = data;
      }
      await loadPreviews();
    } catch (e) { showError(e); } finally { pollRunning = false; }
  }
  async function refreshTask() {
    await action('刷新任务', async () => {
      if (session.value) await pollSession();
      if (task.value) task.value = await request(`/tasks/${task.value.task_id}`);
      await loadPreviews();
    });
  }
  async function loadHistory(append = false) {
    await action('读取历史记录', async () => {
      view.value = 'history'; await refresh();
      const page = await request(`/tasks?limit=20${append && cursor.value ? `&cursor=${encodeURIComponent(cursor.value)}` : ''}`);
      history.value = append ? [...history.value, ...page.items.filter(t => !history.value.some(h => h.task_id === t.task_id))] : page.items;
      cursor.value = page.next_cursor; historyLoaded.value = true;
    });
  }
  async function openTask(id) {
    await action('读取任务', async () => {
      const result = await request(`/tasks/${encodeURIComponent(id)}`);
      clearFlow(); task.value = result;
      view.value = 'create'; await loadPreviews(); persist();
    });
  }
  async function retry(items) {
    if (pending.value) return;
    const selected = items.filter(retryable);
    if (!selected.length) return;
    await action('重试失败图片', async () => {
      await refresh();
      pending.value = { kind: 'retry', key: crypto.randomUUID(), path: `/tasks/${task.value.task_id}/retry`, body: { failed_item_ids: selected.map(i => i.task_item_id) } };
      persist(); await sendPending();
    });
  }
  async function download(item) {
    if (downloadBusy.value || busy.value || item.status !== 'succeeded' || !item.image_id) return;
    downloadBusy.value = true; error.value = null;
    try {
      const id = task.value.task_id;
      await refresh();
      const epoch = identityGeneration();
      const { blob, headers } = await request(`/tasks/${id}/images/${item.image_id}/original`, { binary: true, timeout: 180000 });
      const extension = originalExtension(blob, headers);
      if (epoch !== identityGeneration()) throw new StaleIdentityError();
      const url = URL.createObjectURL(blob), link = document.createElement('a');
      link.href = url; link.download = `task_${id}_image_${item.image_id}.${extension}`;
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      notice.value = '原图已完整读取，下载已开始。';
    } catch (e) { showError(e); } finally { downloadBusy.value = false; }
  }
  async function resetIdentity() {
    await action('重置访客身份', async () => {
      await cookieLock(async () => {
        const next = await request('/auth/guest/reset', { method: 'POST' });
        invalidateIdentity(); identity.value = next; broadcast();
      });
      resetting.value = false; notice.value = '已建立新的访客身份。';
      await checkCapabilities();
    });
  }
  async function cancelSession() {
    await action('取消未提交流程', async () => {
      await refresh();
      const result = await request(`/business-sessions/${session.value.business_session_id}/cancel`, { method: 'POST' });
      session.value = { ...session.value, status: result.status, selection_ready: false };
    });
  }
  function newProject() { if (pending.value) return; clearFlow(); view.value = 'create'; error.value = null; notice.value = ''; }
  onMounted(async () => {
    await connect();
    if (closed) return;
    timer = setInterval(poll, 3000);
    identityTimer = setInterval(async () => {
      if (!identity.value || document.hidden || busy.value) return;
      try {
        const me = await request('/auth/me');
        if (me.user_id !== identity.value?.user_id) { invalidateIdentity(); identity.value = me; }
      } catch (e) { showError(e); }
    }, 30000);
  });
  onBeforeUnmount(() => { closed = true; clearInterval(timer); clearInterval(identityTimer); channel?.close(); unsubscribe(); clearFiles(); revokePreviews(); });
  return {
    mode, identity, busy, error, notice, view, category, primary, auxiliaries, reference, recommendation, session, task,
    history, cursor, historyLoaded, historyFilter, filteredHistory, modes, custom, platform, imageTypes, options,
    canSelect, canNewRecommendation, step, pending, downloadBusy, previews, previewErrors, modalImage, resetting, tasksAvailable, sessionAvailable, failedItems,
    connect, chooseFiles, removeFile, upload, newRecommendation, submit, replayPending, refreshTask, loadHistory, openTask,
    retry, download, resetIdentity, cancelSession, newProject, reloadPreview,
  };
}
