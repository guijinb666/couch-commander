import { newId } from './state.js';

export const TEMPLATES = [
  {
    id: 'tpl-douyin', name: '抖音', app_id: 'app-1', esc: '退出全屏',
    keys: { A: ['b'], B: ['j'], C: ['h'] },
    labels: { A: '开关弹幕', B: '清屏', C: '全屏' },
    note: 'A 弹幕开关 B、B 清屏 J、C 全屏 H；Esc 退出全屏。',
  },
  {
    id: 'tpl-bilibili', name: 'Bilibili 视频', app_id: 'app-2', esc: '退出全屏',
    keys: { A: ['d'], B: ['w'], C: ['f'] },
    labels: { A: '开关弹幕', B: '网页全屏', C: '全屏' },
    note: 'Esc 退出全屏。',
  },
  {
    id: 'tpl-tencent', name: '腾讯视频', app_id: 'app-5', esc: '退出全屏',
    keys: { A: null, B: null, C: ['f11'] },
    labels: { A: '弹幕开关', B: '网页全屏', C: '全屏' },
    note: 'A/B 槽位该平台无原生键，可自行录制或改用鼠标；C 槽位使用浏览器 F11 全屏。',
  },
];

export function applyTemplate(template, activePageId) {
  const hotkeys = [];
  for (const slot of ['A', 'B', 'C']) {
    const keys = template.keys[slot];
    if (!keys) continue;
    hotkeys.push({
      id: newId('hk'),
      name: template.labels[slot] || template.name,
      icon: '',
      keys: [...keys],
      page_id: activePageId || null,
      slot,
    });
  }
  return hotkeys;
}
