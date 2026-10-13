export const fields = [
  { key: 'language', label: '文案语言', note: '图片中呈现的文字语言' },
  { key: 'usage_scene', label: '使用场景', note: '让商品出现在合适的环境里' },
  { key: 'visual_style', label: '视觉风格', note: '确定画面的整体表达' },
  { key: 'selling_points', label: '商品卖点', note: '选择你希望突出的价值' },
];
export const statuses = {
  queued: '排队中', prompting: '构建画面方案', needs_input: '等待处理', generating: '生成中',
  reconciling: '结果核实中', succeeded: '已完成', partially_succeeded: '部分完成', failed: '失败',
  running: '生成中', saving: '保存图片中', analyzing: '分析中', ready: '推荐已就绪',
  starting: '启动流程中', awaiting_recommendation: '分析参考图', recommendation_ready: '推荐已就绪',
  task_created: '任务已创建', expired: '已过期', cancelled: '已取消', cancelling: '取消确认中',
};
export const statusLabel = status => statuses[status] || status || '等待中';
export function recommendationMessage(status, reason) {
  if (reason === 'DIFY_REFERENCE_CONNECTION_UNAVAILABLE') return status === 'failed'
    ? '推荐服务连接失败，自动恢复已结束。请运行一键启动检查服务，再获取新的推荐；参考图已保留。'
    : '推荐服务暂时无法连接，正在有限次数重试；参考图已保留。';
  if (status === 'reconciling' && ['DIFY_REFERENCE_UPLOAD_OUTCOME_UNKNOWN', 'DIFY_REFERENCE_UPLOAD_RESULT_INVALID'].includes(reason))
    return '参考图上传结果无法确认，推荐尚未启动。可结束本次未启动流程后获取新的推荐；已开始的流程不会被取消。';
  if (reason === 'PRESTART_UPLOAD_ABANDONED') return '本次未启动的推荐已结束，参考图仍保留，可获取新的推荐。';
  if (status === 'failed' && reason === 'V34_REFERENCE_NOT_OBSERVED')
    return '分析模型未能读取主参考图，本次推荐已停止。请重试推荐；若仍失败，请新建创作重新上传清晰图片。';
  if (status === 'failed') return ['V34_DIFY_WORKFLOW_FAILED', 'V34_START_ENDED_WITHOUT_FORM'].includes(reason)
    ? '推荐分析未能完成，请点击“获取新的推荐”重试。参考图片已保留，无需重新上传。'
    : '本次推荐失败，请获取新的推荐。参考图片已保留，无需重新上传。';
  if (status === 'reconciling') return reason === 'V34_START_OBSERVATION_EXHAUSTED'
    ? '自动核实暂未确认结果，请刷新状态。为避免重复启动，确认原流程结束后才能重试。'
    : '正在核实原流程是否完成。状态确认后会显示推荐或重试入口，请稍候。';
  if (['expired', 'cancelled'].includes(status)) return '本次流程已结束，请获取新的推荐。';
  return '参考图已上传，正在分析商品并准备推荐。推荐就绪后会自动显示。';
}
export const terminalTask = status => ['succeeded', 'partially_succeeded', 'failed'].includes(status);
export const retryable = item => item.status === 'failed' && !item.image_id && !item.preview_ready &&
  (!item.error || item.error.code === 'IMAGE_GENERATION_FAILED');
export function makePreferences(modes, custom, platform, options) {
  const preferences = { platform };
  if (!options.platform.includes(platform)) throw new Error('请选择有效的发布平台');
  for (const { key, label } of fields) {
    const mode = modes[key];
    if (mode === 'custom') {
      const text = custom[key]?.trim();
      if (!options[key].allow_custom || !text || text.length > 3500) throw new Error(`${label}自定义内容需为 1–3500 字`);
      preferences[key] = { mode, text };
    } else {
      if (!(mode === 'D' && options[key].allow_agent_design) && !options[key].choices.some(x => x.code === mode)) {
        throw new Error(`请选择${label}`);
      }
      preferences[key] = { mode };
    }
  }
  return preferences;
}
export function validateFileMetadata(files) {
  if (!files.length || files.length > 4) throw new Error('请上传 1 张主图和最多 3 张辅图');
  for (const file of files) {
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) throw new Error('仅支持 JPEG、PNG、WebP 图片');
    if (!file.size || file.size > 10 * 1024 * 1024) throw new Error('每张图片需小于或等于 10 MiB，且不能为空');
  }
  if (files.reduce((n, f) => n + f.size, 0) > 40 * 1024 * 1024) throw new Error('整组图片最多 40 MiB');
}
export function originalExtension(blob, headers) {
  const mime = headers.get('content-type')?.split(';')[0].trim();
  const extension = { 'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp' }[mime];
  const length = headers.get('content-length');
  if (!extension || length === null || !/^\d+$/.test(length) || blob.size !== Number(length) || !blob.size) {
    throw new Error('下载文件不完整或响应类型无效，请重新下载');
  }
  return extension;
}
