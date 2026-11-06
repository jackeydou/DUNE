import landscape from "@/assets/dune-eclipse.webp"

import fragmentSource from "./duneAscii.glsl?raw"

const glyphs = " .,:;i+*ox#%@"
const mono = "ui-monospace, Menlo, monospace"
const vertexSource = `#version 300 es
void main() {
  vec2 corner = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  gl_Position = vec4(corner * 2.0 - 1.0, 0.0, 1.0);
}`

type Frame = { time: number; x: number; y: number; strength: number; ripples: Float32Array }
type Size = { width: number; height: number; pixelWidth: number; pixelHeight: number }

function gridFor(image: HTMLImageElement, width: number, height: number) {
  const mobile = window.matchMedia("(max-width: 52rem)").matches
  const cellWidth = Math.max(mobile ? 6.3 : 8, width / 216)
  const sample = document.createElement("canvas")
  sample.width = Math.max(1, Math.round(width / cellWidth))
  sample.height = Math.max(1, Math.round(height / (cellWidth * 1.46)))
  const ctx = sample.getContext("2d")!

  const artHeight = mobile ? Math.min(height, Math.max(448, Math.min(width * 0.95, 736))) : height
  const scale = Math.max(width / image.width, artHeight / image.height)
  const artWidth = image.width * scale
  const x = (width - artWidth) * (mobile ? 0.37 : 0.5)
  const y = mobile ? 0 : (height - image.height * scale) * 0.5
  ctx.scale(sample.width / width, sample.height / height)
  ctx.beginPath()
  ctx.rect(0, 0, width, artHeight)
  ctx.clip()
  ctx.drawImage(image, x, y, artWidth, image.height * scale)

  // Each texel is ink color (RGB) and its character's index (A).
  const grid = ctx.getImageData(0, 0, sample.width, sample.height)
  for (let i = 0; i < grid.data.length; i += 4) {
    const light = (0.52 * grid.data[i] + 0.36 * grid.data[i + 1] + 0.12 * grid.data[i + 2]) / 255
    const tone = Math.max(0, Math.min(1, light ** 0.77 * 1.58 - 0.146))
    grid.data[i] = 90 + tone * 165
    grid.data[i + 1] = 40 + tone ** 1.27 * 174
    grid.data[i + 2] = 23 + tone ** 1.58 * 128
    grid.data[i + 3] = Math.floor(tone * (glyphs.length - 1) + 0.45)
  }
  return grid
}

function glyphAtlas() {
  const canvas = document.createElement("canvas")
  canvas.width = glyphs.length * 18
  canvas.height = 26
  const ctx = canvas.getContext("2d")!
  ctx.font = `500 22px ${mono}`
  ctx.textAlign = "center"
  ctx.textBaseline = "middle"
  ctx.fillStyle = "#fff"
  Array.from(glyphs).forEach((glyph, i) => ctx.fillText(glyph, i * 18 + 9, 13))
  return canvas
}

function compile(gl: WebGL2RenderingContext, type: number, source: string) {
  const shader = gl.createShader(type)
  if (!shader) return null
  gl.shaderSource(shader, source)
  gl.compileShader(shader)
  if (gl.getShaderParameter(shader, gl.COMPILE_STATUS)) return shader
  gl.deleteShader(shader)
  return null
}

function createProgram(gl: WebGL2RenderingContext) {
  const vertex = compile(gl, gl.VERTEX_SHADER, vertexSource)
  const fragment = compile(gl, gl.FRAGMENT_SHADER, fragmentSource)
  const program = vertex && fragment && gl.createProgram()
  if (vertex && fragment && program) {
    gl.attachShader(program, vertex)
    gl.attachShader(program, fragment)
    gl.linkProgram(program)
  }
  gl.deleteShader(vertex)
  gl.deleteShader(fragment)
  if (!program || gl.getProgramParameter(program, gl.LINK_STATUS)) return program
  gl.deleteProgram(program)
  return null
}

function makeTexture(gl: WebGL2RenderingContext, unit: number, filter: number) {
  const texture = gl.createTexture()
  if (!texture) return null
  gl.activeTexture(gl.TEXTURE0 + unit)
  gl.bindTexture(gl.TEXTURE_2D, texture)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
  return texture
}

function webglRenderer(canvas: HTMLCanvasElement) {
  const gl = canvas.getContext("webgl2", {
    alpha: true,
    antialias: false,
    depth: false,
    premultipliedAlpha: false,
    powerPreference: "low-power",
  })
  if (!gl) return null
  const program = createProgram(gl)
  if (!program) return null
  const scene = makeTexture(gl, 0, gl.NEAREST)
  const alphabet = makeTexture(gl, 1, gl.LINEAR)
  if (!scene || !alphabet) {
    gl.deleteTexture(scene)
    gl.deleteTexture(alphabet)
    gl.deleteProgram(program)
    return null
  }

  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, glyphAtlas())
  gl.useProgram(program)
  gl.uniform1i(gl.getUniformLocation(program, "scene"), 0)
  gl.uniform1i(gl.getUniformLocation(program, "alphabet"), 1)
  const resolution = gl.getUniformLocation(program, "resolution")
  const size = gl.getUniformLocation(program, "size")
  const time = gl.getUniformLocation(program, "time")
  const cursor = gl.getUniformLocation(program, "cursor")
  const ripples = gl.getUniformLocation(program, "ripples[0]")

  return {
    resize(dimensions: Size, grid: ImageData) {
      canvas.width = dimensions.pixelWidth
      canvas.height = dimensions.pixelHeight
      gl.viewport(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight)
      gl.uniform2f(resolution, gl.drawingBufferWidth, gl.drawingBufferHeight)
      gl.uniform2f(size, dimensions.width, dimensions.height)
      gl.activeTexture(gl.TEXTURE0)
      // The last byte is an index, so upload the raw bytes without alpha conversion.
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, grid.width, grid.height, 0, gl.RGBA, gl.UNSIGNED_BYTE, grid.data)
    },
    draw(frame: Frame) {
      gl.uniform1f(time, frame.time)
      gl.uniform3f(cursor, frame.x, frame.y, frame.strength)
      gl.uniform3fv(ripples, frame.ripples)
      gl.drawArrays(gl.TRIANGLES, 0, 3)
    },
    dispose() {
      gl.useProgram(null)
      gl.deleteTexture(alphabet)
      gl.deleteTexture(scene)
      gl.deleteProgram(program)
    },
  }
}

function drawStill(canvas: HTMLCanvasElement, size: Size, grid: ImageData) {
  canvas.width = size.pixelWidth
  canvas.height = size.pixelHeight
  const ctx = canvas.getContext("2d")
  if (!ctx) return
  const cellWidth = canvas.width / grid.width
  const cellHeight = canvas.height / grid.height
  ctx.textAlign = "center"
  ctx.textBaseline = "middle"
  ctx.font = `500 ${cellHeight * 22 / 26}px ${mono}`
  for (let row = 0; row < grid.height; row++) {
    for (let column = 0; column < grid.width; column++) {
      const i = (row * grid.width + column) * 4
      const glyph = grid.data[i + 3]
      if (glyph === 0) continue
      ctx.fillStyle = `rgb(${grid.data[i]},${grid.data[i + 1]},${grid.data[i + 2]})`
      ctx.fillText(glyphs[glyph], (column + 0.5) * cellWidth, (row + 0.5) * cellHeight)
    }
  }
}

export function mountDuneAscii(canvas: HTMLCanvasElement, still: HTMLCanvasElement, surface: HTMLElement) {
  const renderer = webglRenderer(canvas)
  const ripples = new Float32Array(5 * 3).fill(-10)
  const frame: Frame = { time: 0, x: 0, y: 0, strength: 0, ripples }
  const image = new Image()
  const start = performance.now()
  let enabled = false
  let ready = false
  let over = false
  let animation = 0
  let rippleIndex = 0
  let lastRipple = -10
  let lastX = -100
  let lastY = -100
  let contextLost = false
  let lastFrame = -Infinity

  const seconds = (now = performance.now()) => (now - start) / 1000

  function schedule() {
    if (ready && renderer && !contextLost && !document.hidden && !animation) {
      animation = requestAnimationFrame(draw)
    }
  }

  function draw(now: number) {
    animation = 0
    if (enabled && now - lastFrame < 32) {
      schedule()
      return
    }
    lastFrame = now
    frame.time = seconds(now)
    frame.strength = enabled && over ? 1 : Math.max(0, frame.strength - 0.16)
    renderer?.draw(frame)
    if (frame.strength > 0 || frame.time - lastRipple < 0.85) schedule()
  }

  function resize() {
    if (!image.complete || !image.naturalWidth) return
    const { width, height } = canvas.getBoundingClientRect()
    if (width <= 0 || height <= 0) return
    const scale = Math.min(devicePixelRatio, 1.5, 1920 / Math.max(width, height))
    const size = { width, height, pixelWidth: Math.round(width * scale), pixelHeight: Math.round(height * scale) }
    const grid = gridFor(image, width, height)
    if (renderer && !contextLost) {
      renderer.resize(size, grid)
      renderer.draw(frame)
      surface.dataset.ascii = "interactive"
    } else {
      drawStill(still, size, grid)
      surface.dataset.ascii = "static"
    }
    ready = true
  }

  function interact(event: PointerEvent) {
    if (!enabled || !renderer || contextLost) return
    if (event.target instanceof Element && event.target.closest(".dune-header, .dune-access, .dune-footer")) {
      if (over) leave()
      return
    }
    const touch = event.pointerType === "touch"
    if (touch && event.type !== "pointerdown") return
    const rect = canvas.getBoundingClientRect()
    frame.x = event.clientX - rect.left
    frame.y = event.clientY - rect.top
    over = !touch
    const now = seconds()
    const moved = Math.hypot(frame.x - lastX, frame.y - lastY)
    if (touch || (moved > 34 && now - lastRipple > 0.09)) {
      ripples.set([frame.x, frame.y, now], rippleIndex * 3)
      rippleIndex = (rippleIndex + 1) % (ripples.length / 3)
      lastRipple = now
      lastX = frame.x
      lastY = frame.y
    }
    schedule()
  }

  function leave() {
    over = false
    schedule()
  }

  function lost() {
    contextLost = true
    cancelAnimationFrame(animation)
    animation = 0
    resize()
  }

  function visibility() {
    if (document.hidden) {
      cancelAnimationFrame(animation)
      animation = 0
      over = false
    } else {
      schedule()
    }
  }

  const observer = new ResizeObserver(resize)
  observer.observe(canvas)
  image.addEventListener("load", resize)
  image.src = landscape
  canvas.addEventListener("webglcontextlost", lost)
  surface.addEventListener("pointermove", interact, { passive: true })
  surface.addEventListener("pointerdown", interact, { passive: true })
  surface.addEventListener("pointerleave", leave)
  document.addEventListener("visibilitychange", visibility)

  return {
    setEnabled(active: boolean) {
      enabled = active
      if (active) return
      over = false
      frame.strength = 0
      lastRipple = -10
      ripples.fill(-10)
      schedule()
    },
    dispose() {
      cancelAnimationFrame(animation)
      observer.disconnect()
      image.removeEventListener("load", resize)
      canvas.removeEventListener("webglcontextlost", lost)
      surface.removeEventListener("pointermove", interact)
      surface.removeEventListener("pointerdown", interact)
      surface.removeEventListener("pointerleave", leave)
      document.removeEventListener("visibilitychange", visibility)
      renderer?.dispose()
    },
  }
}
