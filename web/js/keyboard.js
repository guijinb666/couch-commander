/** 虚拟键盘：字母页顶部固定一排数字键，键盘整体尺寸不变。 */
const NUMBER_ROW = '1234567890';
const LETTERS = ['qwertyuiop', 'asdfghjkl', 'zxcvbnm'];
const NUMBERS = ['1234567890', '-/:;()', '.,?!\'"'];

export class Keyboard {
  constructor(element, connection, { onClose }) {
    this.element = element;
    this.connection = connection;
    this.onClose = onClose;
    this.shift = false;
    this.mode = 'letters';
    this.chinese = false;
    this.render();
  }

  key(label, value, className = '', aria = '') {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `keyboard-key ${className}`;
    button.dataset.key = value;
    button.setAttribute('aria-label', aria || label);
    button.textContent = label;
    button.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      event.preventDefault();
      this.press(value, button);
    });
    return button;
  }

  special(symbol, value, className, aria) {
    const button = this.key('', value, className, aria);
    button.innerHTML = `<svg class="icon" aria-hidden="true"><use href="#${symbol}"/></svg>`;
    return button;
  }

  row(className = '') {
    const row = document.createElement('div');
    row.className = `keyboard-row ${className}`.trim();
    return row;
  }

  /** 字母页：数字行 + 三行字母（末行带 shift / backspace）。 */
  renderLetters() {
    const digits = this.row('keyboard-row-digits');
    [...NUMBER_ROW].forEach(digit => digits.append(this.key(digit, digit, 'digit')));
    this.element.append(digits);

    LETTERS.forEach((letters, rowIndex) => {
      const row = this.row();
      const last = rowIndex === LETTERS.length - 1;
      if (last) {
        const shift = this.special('i-keyboard', 'shift', 'shift', '切换大写');
        shift.innerHTML = '<svg viewBox="0 0 32 32" class="icon" aria-hidden="true"><path d="M16 4 5 15h7v11h8V15h7z" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linejoin="round"/></svg>';
        shift.classList.toggle('active', this.shift);
        row.append(shift);
      }
      [...letters].forEach(letter => row.append(this.key(this.shift ? letter.toUpperCase() : letter, letter)));
      if (last) row.append(this.special('i-close', 'backspace', 'special', '退格'));
      this.element.append(row);
    });
  }

  /** 数字/符号页：沿用原三行结构（第一行就是数字）。 */
  renderNumbers() {
    NUMBERS.forEach((letters, rowIndex) => {
      const row = this.row();
      if (rowIndex === 2) row.append(this.key('#+=', 'symbols', 'special', '更多符号'));
      [...letters].forEach(letter => row.append(this.key(letter, letter)));
      if (rowIndex === 2) row.append(this.special('i-close', 'backspace', 'special', '退格'));
      this.element.append(row);
    });
  }

  bottomRow(modeLabel) {
    const bottom = this.row();
    bottom.append(this.key(modeLabel, 'mode', 'numeric', '切换字母和数字'));
    // 第二个键是中英文切换（🌐），只切输入法并保留视觉态，不收起键盘；收起入口是 #keyboard-toggle。
    bottom.append(this.special('i-globe', 'language', `globe${this.chinese ? ' active' : ''}`, '切换中英文输入'));
    bottom.append(this.key('space', 'space', 'space', '空格'));
    // 最右（最后一个）键固定为回车 return → key.press {keys:['enter']}。
    bottom.append(this.key('return', 'enter', 'return', '回车'));
    this.element.append(bottom);
  }

  render() {
    this.element.replaceChildren();
    if (this.mode === 'letters') {
      this.renderLetters();
      this.bottomRow('123');
      // data-rows 让 CSS 按行数收紧字号与间距，保证键盘总高度不变。
      this.element.dataset.rows = '5';
    } else {
      this.renderNumbers();
      this.bottomRow('ABC');
      this.element.dataset.rows = '4';
    }
    delete this.element.dataset.symbols;
  }

  press(value, button) {
    if (value === 'letters') {
      this.mode = 'letters';
      this.render();
      return;
    }
    button.classList.add('pressed');
    setTimeout(() => button.classList.remove('pressed'), 90);
    if (value === 'shift') {
      this.shift = !this.shift;
      this.render();
    } else if (value === 'mode') {
      this.mode = this.mode === 'letters' ? 'numbers' : 'letters';
      this.render();
    } else if (value === 'symbols') {
      this.mode = 'symbols';
      this.renderSymbols();
    } else if (value === 'language') {
      // 中英文切换：发 shift 切换电脑输入法，并保留键位视觉态；不收起键盘。
      this.chinese = !this.chinese;
      this.connection.send('key.press', { keys: ['shift'] });
      this.render();
    } else if (value === 'space') {
      this.connection.send('key.press', { keys: ['space'] });
    } else if (value === 'enter') {
      this.connection.send('key.press', { keys: ['enter'] });
    } else if (value === 'backspace') {
      this.connection.send('key.press', { keys: ['backspace'] });
    } else if (this.shift && /^[a-z]$/.test(value)) {
      this.connection.send('key.type', { text: value.toUpperCase() });
      this.shift = false;
      this.render();
    } else {
      const supportedKey = /^[a-z0-9]$/.test(value) || ['`', '-', '=', '[', ']', '\\', ';', "'", ',', '.', '/'].includes(value);
      if (supportedKey) this.connection.send('key.press', { keys: [value] });
      else this.connection.send('key.type', { text: value });
    }
  }

  renderSymbols() {
    this.element.replaceChildren();
    ['[]{}#%^*+=', '_\\|~<>€£•'].forEach(letters => {
      const row = this.row();
      [...letters].forEach(value => row.append(this.key(value, value)));
      this.element.append(row);
    });
    const row = this.row();
    row.append(this.key('123', 'mode', 'numeric', '返回数字'));
    row.append(this.special('i-close', 'backspace', 'special', '退格'));
    this.element.append(row);
    const bottom = this.row();
    bottom.append(this.key('ABC', 'letters', 'numeric', '返回字母'));
    bottom.append(this.special('i-globe', 'language', `globe${this.chinese ? ' active' : ''}`, '切换中英文输入'));
    bottom.append(this.key('space', 'space', 'space', '空格'));
    bottom.append(this.key('return', 'enter', 'return', '回车'));
    this.element.append(bottom);
    this.element.dataset.rows = '4';
    this.element.dataset.symbols = '1';
  }
}
