<script setup>
import { computed, nextTick, watch } from 'vue';
import { useStudio } from './useStudio.js';
import { fields, statusLabel, retryable } from './domain.js';
const studio = useStudio();
const { identity, busy, error, notice, view, category, primary, auxiliaries, reference, recommendation,
  session, task, history, cursor, historyLoaded, historyFilter, filteredHistory, modes, custom, platform,
  imageTypes, options, canSelect, canNewRecommendation, step, pending, downloadBusy, previews, previewErrors, modalImage,
  resetting, tasksAvailable, sessionAvailable, failedItems } = studio;
const mockRuntime = import.meta.env.VITE_RUNTIME_LABEL || '本地服务';
const progress = computed(() => task.value?.counts?.planned ? Math.round((task.value.counts.succeeded + task.value.counts.failed) / task.value.counts.planned * 100) : 0);
const complete = computed(() => ['succeeded', 'partially_succeeded', 'failed'].includes(task.value?.status));
const date = value => new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
const shortId = value => value?.slice(0, 8);
let previousFocus;
watch([modalImage, resetting], async ([image, reset]) => {
  if (image || reset) {
    previousFocus = document.activeElement;
    await nextTick(); document.querySelector('dialog[open] button')?.focus();
  } else previousFocus?.focus();
});
function closeDialog() { modalImage.value = null; resetting.value = false; }
function trapFocus(event) {
  if (event.key === 'Escape') { closeDialog(); return; }
  if (event.key !== 'Tab') return;
  const elements = [...event.currentTarget.querySelectorAll('button:not(:disabled), input, a[href]')];
  const first = elements[0], last = elements.at(-1);
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
}
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <a class="brand" href="#" @click.prevent="view = 'create'"><span class="brand-icon">✦</span><span>商品视觉<span class="brand-sub">VISUAL STUDIO</span></span></a>
      <div class="nav-label">工作空间</div>
      <nav aria-label="主导航">
        <button :class="['nav-item', { active: view === 'create' }]" @click="view = 'create'"><span>◇</span>创作工作台</button>
        <button :class="['nav-item', { active: view === 'history' }]" :disabled="!!busy || !identity || !tasksAvailable" @click="studio.loadHistory()"><span>◷</span>历史记录</button>
      </nav>
      <div class="sidebar-note"><span class="tiny-tag">创作提示</span><p>清晰的主图，是好画面的起点。</p><small>搭配细节或场景辅图，让商品信息更完整。</small></div>
      <div class="session-info"><span class="session-dot" :class="{ online: identity }"></span><strong>{{ identity ? '访客会话已连接' : '等待连接' }}</strong><small>历史仅属于当前浏览器身份</small><button v-if="identity" class="text-button" :disabled="!!busy || downloadBusy" @click="resetting = true">重置访客身份</button></div>
    </aside>

    <div class="workspace">
      <header class="topbar"><div><span class="breadcrumb">工作空间</span><span class="separator">/</span>{{ view === 'history' ? '历史记录' : '创作工作台' }}</div><div class="topbar-right"><span class="runtime-tag"><i></i>{{ mockRuntime }}</span><button v-if="identity" class="mobile-session text-button" :disabled="!!busy || downloadBusy" @click="resetting = true">访客设置</button></div></header>
      <main>
        <div class="page-heading"><div><div class="eyebrow">PRODUCT VISUAL CREATION</div><h1>{{ view === 'history' ? '你的创作记录' : task ? '画面正在成为现实' : '让商品，自带吸引力。' }}</h1><p>{{ view === 'history' ? '查看任务进度，预览作品，下载已完成的原图。' : '从一组参考图出发，打造适合你商品的视觉内容。' }}</p></div><button v-if="reference || task" class="button secondary" :disabled="!!busy || !!pending || downloadBusy" @click="studio.newProject()">＋ 新建创作</button></div>

        <div v-if="error" class="alert error" role="alert"><div><strong>{{ error.message }}</strong><small v-if="error.code">{{ error.code }}<template v-if="error.requestId"> · 请求编号 {{ error.requestId }}</template></small></div><button aria-label="关闭错误提示" @click="error = null">×</button></div>
        <div v-if="notice" class="alert info" role="status">{{ notice }}</div>
        <div v-if="!identity" class="panel connection-panel"><span class="empty-icon">◎</span><h2>连接你的创作空间</h2><p>使用浏览器访客会话保存任务和历史记录。</p><button class="button primary" :disabled="!!busy" @click="studio.connect()">{{ busy || '重新连接' }}</button></div>
        <template v-else>
          <div v-if="!tasksAvailable" class="alert warning">当前后端未开放任务接口，可上传参考图并查看推荐。完整生图和历史记录需要启用对应后端服务。</div>
          <div v-if="!sessionAvailable" class="alert warning">当前后端未开放 Chatflow 会话接口，请检查服务地址和部署配置。</div>
          <div v-if="pending" class="alert warning pending"><div><strong>原操作结果尚未确认</strong><p>重新确认会复用原请求内容与幂等编号。请先确认原操作，不要重复创建新任务。</p></div><button class="button secondary" :disabled="!!busy" @click="studio.replayPending()">确认原操作</button></div>

          <template v-if="view === 'create'">
            <ol class="steps" aria-label="创作步骤"><li v-for="(title, index) in ['上传参考图', '确认画面偏好', '生成与下载']" :key="title" :class="{ current: step === index + 1, done: step > index + 1 }"><span>{{ step > index + 1 ? '✓' : `0${index + 1}` }}</span><strong>{{ title }}</strong></li></ol>

            <div v-if="!reference && !task" class="create-layout">
              <section class="panel upload-panel"><div class="section-heading"><div><span class="section-number">01</span><h2>添加商品参考图</h2></div><span class="soft-tag">1 张主图 · 最多 3 张辅图</span></div>
                <form @submit.prevent="studio.upload()">
                  <label class="field-label" for="category">商品类别 <span>*</span></label><input id="category" v-model="category" placeholder="例如：手提包、运动鞋、咖啡杯" maxlength="100" required :disabled="!!busy" /><p class="field-hint">填写具体类别，帮助推荐更贴合你的商品。</p>
                  <label class="field-label" for="primary-image">商品主图 <span>*</span></label>
                  <label class="upload-zone" :class="{ filled: primary }" for="primary-image"><img v-if="primary" :src="primary.url" alt="商品主图预览" /><template v-else><span class="upload-symbol">↥</span><strong>选择一张商品主图</strong><span>正面、完整、清晰的商品照片</span><small>JPEG / PNG / WebP · 单张 ≤ 10 MiB</small></template><span v-if="primary" class="replace-image">点击更换主图</span></label><input id="primary-image" class="file-input" type="file" accept="image/jpeg,image/png,image/webp" :disabled="!!busy" @change="studio.chooseFiles($event, 'primary')" />
                  <label class="field-label auxiliary-label" for="aux-images">辅助参考图 <span class="optional">可选</span></label>
                  <div class="aux-grid"><div v-for="(file, index) in auxiliaries" :key="file.url" class="aux-preview"><img :src="file.url" :alt="`辅图 ${index + 1}`" /><button type="button" :aria-label="`移除辅图 ${index + 1}`" :disabled="!!busy" @click="studio.removeFile(index)">×</button><small>辅图 {{ index + 1 }}</small></div><label v-if="auxiliaries.length < 3" class="aux-add" for="aux-images"><span>＋</span>添加辅图</label></div><input id="aux-images" class="file-input" type="file" accept="image/jpeg,image/png,image/webp" multiple :disabled="!!busy" @change="studio.chooseFiles($event, 'aux')" />
                  <div class="form-footer"><span>整组 ≤ 40 MiB · 单图 ≤ 2500 万像素</span><button class="button primary" :disabled="!!busy || !primary || !category.trim()">{{ busy || '获取画面推荐' }} <span v-if="!busy">→</span></button></div>
                </form>
              </section>
              <aside class="guide-column"><section class="guide-card"><div class="guide-art" aria-hidden="true"><div class="art-orbit"></div><div class="art-card card-back">✧</div><div class="art-card card-front"><span>YOUR PRODUCT</span><div class="abstract-product"></div><small>Made to stand out.</small></div><span class="art-spark">✦</span></div><span class="tiny-tag">从参考，到作品</span><h3>把商品信息<br />变成视觉表达。</h3><p>推荐语言、使用场景、风格和卖点。你只需确认一次偏好，后续进度由工作台持续跟踪。</p></section><section class="tips"><h3>上传小建议</h3><p><span>01</span>主图保持商品完整，避免遮挡</p><p><span>02</span>辅图可补充细节、材质或使用场景</p><p><span>03</span>同一组图片应展示同一个商品</p></section></aside>
            </div>

            <section v-else-if="!task" class="panel preferences-panel"><div class="section-heading"><div><span class="section-number">02</span><h2>{{ options ? '确认你的画面偏好' : '正在准备画面推荐' }}</h2></div><span class="soft-tag">{{ category || reference?.product_category }}</span></div>
              <div v-if="!options" class="empty-state"><div class="loading-orbit" v-if="['analyzing', 'starting', 'awaiting_recommendation'].includes(recommendation?.status || session?.status)"></div><h3>{{ statusLabel(session?.status || recommendation?.status) }}</h3><p>参考图已上传。推荐结果就绪后，会在这里显示。</p><small v-if="session?.safe_reason_code">{{ session.safe_reason_code }}</small><button v-if="canNewRecommendation" class="button secondary" :disabled="!!busy || !!pending || (session && session.status === 'reconciling')" @click="studio.newRecommendation()">获取新的推荐</button></div>
              <form v-else @submit.prevent="studio.submit()">
                <div v-if="session && !session.selection_ready && !['failed', 'expired', 'cancelled'].includes(session.status)" class="alert info">正在等待后端确认表单可提交，请稍候。</div>
                <div v-if="['expired', 'failed', 'cancelled'].includes(session?.status || recommendation?.status)" class="alert warning">本次推荐已不可提交，请新建创作或获取新推荐。</div>
                <div class="platform-field"><label class="field-label" for="platform">发布平台</label><select id="platform" v-model="platform" :disabled="!canSelect || !!busy"><option v-for="name in options.platform" :key="name">{{ name }}</option></select></div>
                <fieldset v-for="field in fields" :key="field.key" :disabled="!canSelect || !!busy" class="preference-field"><legend>{{ field.label }}<small>{{ field.note }}</small></legend><div class="choice-grid"><label v-for="choice in options[field.key].choices" :key="choice.code" class="choice-card" :class="{ selected: modes[field.key] === choice.code }"><input v-model="modes[field.key]" type="radio" :name="field.key" :value="choice.code" /><span class="choice-code">{{ choice.code }}</span><span>{{ choice.text }}</span></label></div><div class="extra-choices"><label v-if="options[field.key].allow_agent_design"><input v-model="modes[field.key]" type="radio" :name="field.key" value="D" />由智能体设计</label><label v-if="options[field.key].allow_custom"><input v-model="modes[field.key]" type="radio" :name="field.key" value="custom" />自定义</label></div><textarea v-if="modes[field.key] === 'custom'" v-model="custom[field.key]" :aria-label="`自定义${field.label}`" maxlength="3500" rows="3" required :placeholder="`写下你的${field.label}要求`"></textarea></fieldset>
                <fieldset class="image-type-field" :disabled="!canSelect || !!busy"><legend>想生成哪些画面？<small>每种类型生成 1 张，最多 8 张</small></legend><div class="type-grid"><label v-for="(type, index) in recommendation.image_types" :key="type" :class="['type-option', { selected: imageTypes.includes(type) }]"><input v-model="imageTypes" type="checkbox" :value="type" /><span>{{ String(index + 1).padStart(2, '0') }}</span>{{ type }}</label></div></fieldset>
                <div class="form-footer"><span>已选择 <strong>{{ imageTypes.length }}</strong> 张画面<small v-if="recommendation.valid_until"> · 推荐有效至 {{ date(recommendation.valid_until) }}</small></span><button class="button primary" :disabled="!canSelect || !!busy || !imageTypes.length || !tasksAvailable">{{ busy || `确认并生成 ${imageTypes.length} 张` }} <span v-if="!busy">→</span></button></div>
              </form>
              <div v-if="canNewRecommendation && options" class="session-actions"><button class="button secondary" :disabled="!!busy || !!pending" @click="studio.newRecommendation()">获取新的推荐</button></div>
              <div v-if="session && !task && !pending && !['failed', 'expired', 'cancelled', 'cancelling', 'reconciling'].includes(session.status)" class="session-actions"><button class="text-button" :disabled="!!busy" @click="studio.cancelSession()">取消未提交的流程</button></div>
            </section>

            <section v-else class="results-section"><div class="panel task-summary"><div><span class="tiny-tag">{{ complete ? 'CREATION RESULT' : 'CREATION IN PROGRESS' }}</span><h2>{{ statusLabel(task.status) }}</h2><p class="task-id">任务 {{ task.task_id }}</p></div><div class="task-metrics"><div><strong>{{ task.counts?.succeeded || 0 }}<small> / {{ task.counts?.planned || 0 }}</small></strong><span>成功画面</span></div><div><strong>{{ task.counts?.failed || 0 }}</strong><span>失败</span></div><div><strong>{{ task.counts?.unresolved || 0 }}</strong><span>待核实</span></div></div><div class="task-controls"><button class="button secondary" :disabled="!!busy" @click="studio.refreshTask()">刷新状态</button><button v-if="failedItems.length" class="button primary" :disabled="!!busy || !!pending" @click="studio.retry(failedItems)">重试全部失败图片</button></div><div class="progress-bar" role="progressbar" :aria-valuenow="progress" aria-valuemin="0" aria-valuemax="100" aria-label="已结束图位比例"><span :style="{ width: `${progress}%` }"></span></div></div>
              <div v-if="task.status === 'reconciling' || task.counts?.unresolved" class="alert info">部分结果仍在核实，请继续查询当前任务。核实中的图片不会重复提交。</div><div v-if="task.status === 'needs_input'" class="alert warning">当前任务返回等待输入状态。新版流程没有补问页面，请联系负责人核对后端流程版本。</div><p class="result-help">任务在后台继续执行，关闭页面后可从历史记录重新查看。成功保存的图片可立即下载。</p>
              <div class="result-grid"><article v-for="item in task.items" :key="item.task_item_id" class="result-card"><div class="result-image"><button v-if="previews[item.image_id]" class="image-button" :aria-label="`放大预览 ${item.image_type}`" @click="modalImage = { url: previews[item.image_id], title: item.image_type }"><img :src="previews[item.image_id]" :alt="item.image_type" /></button><div v-else class="image-placeholder"><span>{{ item.status === 'failed' ? '×' : item.status === 'reconciling' ? '◎' : '✧' }}</span><p>{{ previewErrors[item.image_id] || statusLabel(item.status) }}</p><button v-if="previewErrors[item.image_id]" class="text-button" @click="studio.reloadPreview(item)">重试读取预览</button></div><span class="image-position">{{ String(item.position).padStart(2, '0') }}</span></div><div class="result-content"><div><h3>{{ item.image_type }}</h3><span :class="['status-badge', item.status]">{{ statusLabel(item.status) }}</span></div><p v-if="item.width">{{ item.width }} × {{ item.height }} px</p><p v-if="item.error" class="item-error">{{ item.error.message }}</p><button v-if="item.status === 'succeeded' && item.image_id" class="button secondary full" :disabled="downloadBusy || !!busy" @click="studio.download(item)">{{ downloadBusy ? '正在下载…' : '↓ 下载原图' }}</button><button v-else-if="retryable(item)" class="button secondary full" :disabled="!!busy || !!pending" @click="studio.retry([item])">↻ 重试这张图片</button><p v-else-if="item.status === 'failed'" class="field-hint">本次未发起生图，请核对商品后新建创作。</p></div></article></div>
            </section>
          </template>

          <template v-else><section class="panel history-panel"><div class="section-heading"><div><h2>任务历史</h2><span class="soft-tag">已加载 {{ history.length }} 个任务</span></div><button class="button secondary" :disabled="!!busy" @click="studio.loadHistory()">刷新列表</button></div><div class="filter-tabs" aria-label="历史筛选"><button v-for="filter in [{ value: 'all', text: '全部' }, { value: 'active', text: '进行中' }, { value: 'ended', text: '已结束' }]" :key="filter.value" :class="{ selected: historyFilter === filter.value }" @click="historyFilter = filter.value">{{ filter.text }}</button></div><div v-if="!filteredHistory.length" class="empty-state"><span class="empty-icon">◷</span><h3>{{ historyLoaded ? '这里还没有创作记录' : '读取任务记录中' }}</h3><p>上传参考图，开始你的第一次创作。</p></div><div class="history-list"><button v-for="entry in filteredHistory" :key="entry.task_id" class="history-row" :disabled="!!busy || !!pending" @click="studio.openTask(entry.task_id)"><span class="history-mark">✧</span><div class="history-title"><strong>{{ entry.items[0]?.image_type || '商品视觉任务' }}<template v-if="entry.counts.planned > 1"> 等 {{ entry.counts.planned }} 张</template></strong><small>{{ date(entry.created_at) }} · {{ shortId(entry.task_id) }}</small></div><span class="history-count">{{ entry.counts.succeeded }}/{{ entry.counts.planned }} 成功</span><span :class="['status-badge', entry.status]">{{ statusLabel(entry.status) }}</span><span class="row-arrow">→</span></button></div><div v-if="cursor" class="load-more"><button class="button secondary" :disabled="!!busy" @click="studio.loadHistory(true)">{{ busy || '加载更多' }}</button></div></section><p class="field-hint">默认终态任务保留 60 天，实际可用性由后端判定。重置身份后无法访问旧身份历史。</p></template>
        </template>
        <footer class="page-footer"><span>商品视觉工作台</span><span>访客模式 · 账号登录与支付暂未开放</span></footer>
      </main>
    </div>

    <div v-if="modalImage || resetting" class="modal-backdrop" @click.self="closeDialog"><dialog open aria-modal="true" :aria-label="resetting ? '重置访客身份' : modalImage.title" @keydown="trapFocus"><template v-if="resetting"><h2>重置访客身份？</h2><p>这会清除当前页面的表单与缓存，并建立新身份。旧任务仍继续执行，但新身份无法访问旧历史和图片。</p><div class="dialog-actions"><button class="button secondary" @click="resetting = false">保留当前身份</button><button class="button danger" :disabled="!!busy" @click="studio.resetIdentity()">确认重置</button></div></template><template v-else><div class="dialog-title"><h2>{{ modalImage.title }}</h2><button class="close-button" aria-label="关闭预览" @click="modalImage = null">×</button></div><img class="enlarged-image" :src="modalImage.url" :alt="modalImage.title" /></template></dialog></div>
  </div>
</template>
