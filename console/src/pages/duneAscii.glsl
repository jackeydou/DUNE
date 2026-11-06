#version 300 es
precision highp float;

uniform sampler2D scene;
uniform sampler2D alphabet;
uniform vec2 resolution;
uniform vec2 size;
uniform vec3 cursor;
uniform vec3 ripples[5];
uniform float time;
out vec4 color;

float hash(vec2 p) {
  vec3 q = fract(vec3(p.xyx) * 0.1031);
  q += dot(q, q.yzx + 33.33);
  return fract((q.x + q.y) * q.z);
}

void main() {
  vec2 pixel = vec2(gl_FragCoord.x, resolution.y - gl_FragCoord.y);
  vec2 point = pixel / resolution * size;
  vec2 delta = point - cursor.xy;
  float distanceToCursor = length(delta);
  float reach = clamp(size.x * 0.135, 105.0, 205.0);
  float hover = (1.0 - smoothstep(0.0, reach, distanceToCursor)) * cursor.z;
  vec2 radial = delta / max(1.0, distanceToCursor);
  vec2 tangent = vec2(-radial.y, radial.x);
  vec2 displacement = (radial * 26.0 + tangent * 19.0) * hover;
  float wake = 0.0;

  for (int i = 0; i < 5; i++) {
    float age = time - ripples[i].z;
    if (age < 0.0 || age >= 0.85) continue;
    vec2 waveDelta = point - ripples[i].xy;
    float waveDistance = length(waveDelta);
    float wave = 1.0 - smoothstep(0.0, 22.0, abs(waveDistance - age * 180.0));
    wave *= (1.0 - age / 0.85) * smoothstep(0.0, 0.12, age);
    displacement += waveDelta / max(1.0, waveDistance) * wave * 9.0;
    wake = max(wake, wave);
  }

  ivec2 grid = textureSize(scene, 0);
  vec2 tile = clamp((point - displacement) / size, vec2(0.0), vec2(0.9999)) * vec2(grid);
  ivec2 cell = ivec2(tile);
  vec4 ink = texelFetch(scene, cell, 0);
  float glyph = floor(ink.a * 255.0 + 0.5);
  float count = float(textureSize(alphabet, 0).x / 18);
  float energy = clamp(hover * 1.6 + wake * 0.85, 0.0, 1.0);
  float change = hash(vec2(cell) + floor(time * 11.0) * 13.0);
  if (change < energy * 0.74) {
    glyph = min(count - 1.0, 4.0 + floor(change / max(energy * 0.74, 0.001) * (count - 4.0)));
  }

  // The half-texel inset prevents interpolation from a neighboring character in the atlas.
  vec2 position = mix(vec2(0.5 / 18.0, 0.5 / 26.0), vec2(17.5 / 18.0, 25.5 / 26.0), fract(tile));
  float alpha = texture(alphabet, vec2((glyph + position.x) / count, position.y)).a;
  vec3 tint = mix(ink.rgb, vec3(1.0, 0.80, 0.57), energy * 0.85);
  color = vec4(tint, alpha);
}
