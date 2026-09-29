import { reactive } from 'vue'

/**
 * 浏览器采集设备的启用状态。
 *
 * 「实时检测」页开启/关闭摄像头与麦克风时写入这里，
 * 底部状态栏读取它以显示"摄像头 / 麦克风 是否已开启"。
 * 这两项属于前端本地状态（后端无从得知浏览器有没有授权），
 * 因此不走接口，用一个轻量响应式对象在组件间共享。
 */
export const deviceState = reactive({
  camera: false,
  mic: false
})

export function setCamera(on) {
  deviceState.camera = !!on
}

export function setMic(on) {
  deviceState.mic = !!on
}

/** 停止全部采集时统一复位 */
export function resetDevices() {
  deviceState.camera = false
  deviceState.mic = false
}
