/**
 * 语音按钮：长按 → 手机请求麦克风权限 → 手机端语音识别成文字 → 注入电脑。
 *
 * 为什么长按**不再打开任何页面**：
 *   这一版曾经长按就 ``voice.start{url:'https://muse.ai/'}`` 让电脑打开 muse。
 *   实测该站是 Meta Muse 的落地页、没有语音输入按钮，打开它帮不上忙，还会
 *   突然弹窗打断用户正在做的事 —— YG 明确要求「长按语音还是会弹出 muse，关掉」。
 *   所以现在 ``voice.start{}`` 不带 url，后端也只建内存会话、不碰浏览器。
 *
 * 现在的真实链路：
 *   1) 长按 → 立刻 getUserMedia({audio:true})，这就是"发起麦克风权限请求"，
 *      安卓会弹系统授权框。
 *   2) 权限拿到后，用浏览器自带的语音识别（SpeechRecognition）把手机听到的话
 *      实时转成文字，边识别边发 voice.text{final:false} 作为预览。
 *   3) 松手 → 发 voice.text{final:true}，后端把这段文字用物理键盘注入到
 *      电脑当前焦点输入框，并回车提交。
 *   4) 同时发 voice.stop{} 收尾，不打开、不关闭、不导航任何页面。
 *
 * 浏览器不支持语音识别（部分安卓 WebView）时，如实提示，不假装成功。
 */
export class VoiceButton {
  constructor(element, state, connection, { onToast }) {
    this.element = element;
    this.state = state;
    this.connection = connection;
    this.onToast = onToast;
    this.active = false;
    this.stream = null;
    this.recognition = null;
    this.finalText = '';
    this.interimText = '';
    this.gotAnyResult = false;
    this.bind();
  }

  bind() {
    this.element.addEventListener('pointerdown', event => {
      if (event.button !== 0 || this.active) return;
      event.preventDefault();
      this.start();
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(type => {
      this.element.addEventListener(type, () => this.stop());
    });
  }

  /** 把按钮切到"正在听"的视觉状态。 */
  setRecording(on) {
    this.element.classList.toggle('recording', on);
    this.element.setAttribute('aria-pressed', on ? 'true' : 'false');
  }

  async start() {
    this.active = true;
    this.finalText = '';
    this.interimText = '';
    this.gotAnyResult = false;
    this.setRecording(true);

    // 1) 通知后端开始一段语音会话。**不带 url**：后端不会再打开任何页面
    //    （muse 那条路已下线，用户明确要求不要再弹窗）。
    this.connection.send('voice.start', {});

    // 2) 真正发起麦克风权限请求。HTTP 下 navigator.mediaDevices 不存在，
    //    所以这里要分开提示，别让用户以为是"被拒绝"。
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      this.onToast('当前页面未启用麦克风（需要 HTTPS 或 localhost）。请用启动器给出的 https 地址打开。');
      this.setRecording(false);
      this.active = false;
      return;
    }

    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        video: false,
      });
    } catch (error) {
      const name = error && error.name ? error.name : 'Error';
      if (name === 'NotAllowedError' || name === 'SecurityError') {
        this.onToast('麦克风权限被拒绝：请在浏览器地址栏的站点设置里允许使用麦克风。');
      } else if (name === 'NotFoundError') {
        this.onToast('没有检测到麦克风设备。');
      } else if (name === 'NotReadableError') {
        this.onToast('麦克风被其他应用占用，请关闭后重试。');
      } else {
        this.onToast(`麦克风打开失败：${name}`);
      }
      this.setRecording(false);
      this.active = false;
      return;
    }

    // 用户可能在权限弹窗期间就松手了。
    if (!this.active) {
      this.releaseStream();
      return;
    }

    // 3) 手机端把语音识别成文字推给电脑。
    const started = this.startRecognition();
    if (started) {
      this.onToast('正在听… 松手后这段文字会自动输入到电脑。');
    } else {
      // 没有语音识别能力就如实说：麦克风开着，但这一端不会产生文字。
      this.onToast('此浏览器不支持语音识别，无法把语音转成文字输入电脑。');
    }
  }

  /** 选择可用的语音识别实现；没有返回 false。 */
  startRecognition() {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) return false;
    let recognition;
    try {
      recognition = new Recognition();
    } catch {
      return false;
    }
    recognition.lang = 'zh-CN';
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;

    recognition.onresult = event => {
      this.gotAnyResult = true;
      let interim = '';
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        const text = (result[0] && result[0].transcript) || '';
        if (result.isFinal) {
          this.finalText += text;
          this.connection.send('voice.text', { text: this.finalText, final: false });
        } else {
          interim += text;
        }
      }
      this.interimText = interim;
    };
    recognition.onerror = event => {
      const code = event && event.error ? event.error : 'unknown';
      if (code === 'not-allowed' || code === 'service-not-allowed') {
        this.onToast('语音识别被拒绝：请允许站点使用麦克风与语音识别。');
      } else if (code === 'no-speech') {
        // 很正常：这一小段没听到人说话，不当失败。
      } else if (code === 'aborted') {
        // 松手主动停的，忽略。
      } else {
        this.onToast(`语音识别中断（${code}），请重试。`);
      }
    };
    recognition.onend = () => {
      // 只在自己还在按住时自动续听，避免松手后又偷偷录音。
      if (this.active) {
        try { recognition.start(); } catch { /* 已在跑 */ }
      }
    };
    try {
      recognition.start();
    } catch {
      return false;
    }
    this.recognition = recognition;
    return true;
  }

  releaseStream() {
    if (this.stream) {
      this.stream.getTracks().forEach(track => track.stop());
      this.stream = null;
    }
  }

  stop() {
    if (!this.active) return;
    this.active = false;
    this.setRecording(false);

    if (this.recognition) {
      try { this.recognition.onend = null; } catch { /* ignore */ }
      try { this.recognition.stop(); } catch { /* 已停止 */ }
      this.recognition = null;
    }

    const text = (this.finalText + this.interimText).trim();
    if (text) {
      // 松手即提交：后端把这段文字注入电脑焦点输入框并回车。
      this.connection.send('voice.text', { text, final: true });
    } else if (this.gotAnyResult === false && this.stream) {
      this.onToast('这一句没听到内容，请再试一次。');
    }
    this.releaseStream();
    this.connection.send('voice.stop', {});
  }
}
