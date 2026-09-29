/**
 * PCM 重采样 AudioWorklet 处理器（注册名：pcm-16k）
 *
 * 职责：把 AudioContext 采样率（通常 48000Hz）的单声道 Float32 音频
 *       线性插值重采样到 16000Hz，并转成 16-bit 小端 PCM 发回主线程。
 *
 * 说明：
 * - 只取第 0 声道，避免立体声重复采样；
 * - 用一个持续累加的源位置指针 this.pos 保证跨 block 的相位连续性；
 * - 上一个 block 的最后一个样本 this.prev 用于跨 block 边界插值；
 * - 通过 this.port.postMessage(buffer, [buffer]) 以零拷贝方式回传。
 */
class Pcm16kProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super()
    const opts = (options && options.processorOptions) || {}
    // AudioContext 的采样率（由主线程通过 processorOptions 传入，兜底用全局 sampleRate）
    this.inputSampleRate = opts.inputSampleRate || sampleRate || 48000
    // 目标采样率：后端 Vosk 流式识别要求 16000Hz
    this.targetRate = 16000
    // 每产出一个 16k 样本需要前进的输入样本数
    this.step = this.inputSampleRate / this.targetRate
    // 当前读取位置（以输入样本为单位，跨 block 持续累加）
    this.pos = 0
    // 上一个 block 的最后一个样本，用于跨 block 线性插值
    this.prev = 0
    this.started = false
  }

  process(inputs) {
    const input = inputs[0]
    // 输入为空时直接返回，保持处理器存活
    if (!input || !input.length) return true
    // 只取第 0 声道
    const ch = input[0]
    const n = ch ? ch.length : 0
    if (!n) return true

    // 首个 block 用自身首样本初始化插值基准，避免起始处的阶跃噪声
    if (!this.started) {
      this.prev = ch[0]
      this.started = true
    }

    // 预估输出长度，避免动态数组频繁扩容
    const out = new Int16Array(Math.ceil(n / this.step) + 2)
    let k = 0
    while (this.pos < n) {
      const i = Math.floor(this.pos)
      const frac = this.pos - i
      // 在输入样本 i-1 与 i 之间做线性插值；i=0 时用上一 block 的末样本保证连续
      const s0 = i === 0 ? this.prev : ch[i - 1]
      const s1 = ch[i]
      const v = s0 + (s1 - s0) * frac
      // 限幅到 [-1, 1] 后量化为 Int16（赋值到 Int16Array 会截断取整）
      const s = v < -1 ? -1 : v > 1 ? 1 : v
      out[k++] = s < 0 ? s * 0x8000 : s * 0x7fff
      this.pos += this.step
    }

    // 指针回退到本 block 内，并记录末样本供下一 block 插值
    this.pos -= n
    this.prev = ch[n - 1]

    if (k) {
      // 只发送有效部分，并转移所有权（零拷贝）
      const buffer = out.buffer.slice(0, k * 2)
      this.port.postMessage(buffer, [buffer])
    }
    return true
  }
}

registerProcessor('pcm-16k', Pcm16kProcessor)
