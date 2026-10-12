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
