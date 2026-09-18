/**
 * brain_orb.js — FRIDAY's presence: a neural brain in place of the orb.
 *
 * Ported from threejs-brain-animation. That component loads brain.glb and
 * instances a tiny wireframe box at every vertex; boxes near the cursor swell
 * and spin ("hover"). Here the same technique is driven by session state
 * instead of a pointer, and a particle layer is laid over the same vertices:
 *
 *   idle       slow y rotation, gentle breathing
 *   listening  synaptic pulse waves travelling through the mesh with the mic
 *   thinking   dispersion — every node swells, spins and pushes outward along
 *              its normal, and the palette shifts to magenta
 *   speaking   harmonic contraction pulsing with playback volume
 *   offline    dim, still, ember-tinted
 *
 * Same public surface as the orb it replaces (setState / setLevel / resize /
 * state), so app.js's state machine drives it unchanged. Its third-party
 * dependencies are not carried over: per-instance uniforms are plain
 * InstancedBufferAttributes, and gsap is the same lerp the old orb used.
 *
 * Transparent alpha throughout — the HUD ground shows through.
 */

import * as THREE from "three";
import { GLTFLoader } from "../vendor/GLTFLoader.js";

const MODEL_URL = "assets/models/brain/brain.glb";

// FRIDAY's obsidian palette. Hue is a function of state; audio never tints it.
const PALETTE = {
  cyan:    new THREE.Color("#00F2FE"),   // base particles / resting synapses
  indigo:  new THREE.Color("#4A00E0"),   // deep synaptic matter
  violet:  new THREE.Color("#A18CD1"),   // core highlight
  magenta: new THREE.Color("#F472B6"),   // thinking / firing
  ember:   new THREE.Color("#E5726F"),   // offline
};

// Per-state targets. Everything the animation does is a lerp toward these.
const STATES = {
  idle:      { disperse: 0.0, contract: 0.0, wave: 0.0, spin: 0.12, dim: 1.0,  offline: 0 },
  listening: { disperse: 0.0, contract: 0.0, wave: 1.0, spin: 0.16, dim: 1.0,  offline: 0 },
  thinking:  { disperse: 1.0, contract: 0.0, wave: 0.0, spin: 0.75, dim: 1.05, offline: 0 },
  speaking:  { disperse: 0.0, contract: 1.0, wave: 0.3, spin: 0.22, dim: 1.0,  offline: 0 },
  offline:   { disperse: 0.0, contract: 0.0, wave: 0.0, spin: 0.03, dim: 0.35, offline: 1 },
};

const NODE_EVERY = 3;         // one wireframe node per N vertices; particles use all

// ---------- Shaders ----------

const POINT_VERT = /* glsl */ `
uniform float uTime, uLevel, uDisperse, uContract, uWave, uPixelRatio;
attribute float aPhase, aRandom;
varying float vMix, vFire;

void main() {
  vec3 dir = normalize(position);

  // Breathing: each vertex on its own phase so the surface shimmers, not throbs.
  float breath = 1.0 + sin(uTime * 0.9 + aPhase) * 0.012;
  vec3 p = position * breath;

  // Thinking: scatter outward along the normal, with the reference's jitter.
  float jitter = sin(uTime * 4.0 + aPhase) * cos(uTime * 2.5 + aRandom * 6.2831);
  p += dir * uDisperse * 0.15 * (1.0 + jitter * 0.4);

  // Listening: a synaptic wave front travelling through the mesh, height from the mic.
  float wave = sin(position.y * 9.0 - uTime * 6.0 + aPhase) * 0.5 + 0.5;
  p += dir * uWave * uLevel * 0.06 * wave;

  // Speaking: harmonic contraction against playback volume.
  p *= 1.0 - uContract * uLevel * 0.10;

  vec4 mv = modelViewMatrix * vec4(p, 1.0);
  float size = (2.0 + aRandom * 1.8) * (1.0 + uDisperse * 0.5 + uLevel * 0.35);
  gl_PointSize = size * uPixelRatio * (1.7 / -mv.z);
  gl_Position = projectionMatrix * mv;

  vMix = smoothstep(-0.55, 0.45, position.y);
  vFire = wave * uWave + uDisperse;
}
`;

const POINT_FRAG = /* glsl */ `
uniform vec3 uCyan, uIndigo, uMagenta, uEmber;
uniform float uDisperse, uDim, uOffline;
varying float vMix, vFire;

void main() {
  float d = length(gl_PointCoord - vec2(0.5));
  if (d > 0.5) discard;
  float alpha = smoothstep(0.5, 0.12, d);

  // Cyan at the crown, indigo in the deep matter; magenta takes over as it fires.
  vec3 base = mix(uIndigo, uCyan, vMix);
  vec3 col = mix(base, uMagenta, clamp(uDisperse * 0.85 + vFire * 0.25, 0.0, 1.0));
  col = mix(col, uEmber, uOffline * 0.8);

  gl_FragColor = vec4(col, alpha * 0.85 * uDim);
}
`;

const NODE_VERT = /* glsl */ `
uniform float uTime, uLevel, uDisperse, uContract, uWave;
attribute float aRotation, aSize, aPhase;
varying float vGlow;

#define PI 3.14159265359
mat2 rotate(float a) { float s = sin(a), c = cos(a); return mat2(c, -s, s, c); }

void main() {
  vec3 center = (instanceMatrix * vec4(0.0, 0.0, 0.0, 1.0)).xyz;
  vec3 dir = normalize(center);

  // The reference swells nodes near the pointer: c = smoothstep(dist) then
  // scale += c*8*uHover. That was for a few nodes under the cursor; applied to
  // every node it is toned down so the brain reads as firing, not exploding.
  float c = uDisperse * (0.55 + 0.45 * sin(uTime * 3.0 + aPhase));
  float scale = aSize + c * 3.5;
  vec3 pos = position * scale;

  float spin = uTime * (0.35 + uDisperse * 2.4) * aRotation;
  pos.xz *= rotate(PI * c * aRotation + spin + PI * aRotation * 0.43);
  pos.xy *= rotate(PI * c * aRotation + spin + PI * aRotation * 0.71);

  vec4 world = instanceMatrix * vec4(pos, 1.0);
  float wave = sin(center.y * 9.0 - uTime * 6.0 + aPhase) * 0.5 + 0.5;
  world.xyz += dir * (uDisperse * 0.12 + uWave * uLevel * 0.04 * wave);
  world.xyz *= 1.0 - uContract * uLevel * 0.10;

  gl_Position = projectionMatrix * modelViewMatrix * world;
  vGlow = c;
}
`;

const NODE_FRAG = /* glsl */ `
uniform vec3 uCyan, uViolet, uMagenta, uEmber;
uniform float uDisperse, uDim, uOffline;
varying float vGlow;

void main() {
  vec3 col = mix(mix(uCyan, uViolet, 0.55), uMagenta, uDisperse * 0.9);
  col = mix(col, uEmber, uOffline * 0.8);
  gl_FragColor = vec4(col, (0.22 + vGlow * 0.5) * uDim);
}
`;

// ---------- Component ----------

export class FridayBrainVisualizer {
  constructor(container) {
    this.container = container;
    this._state = "idle";
    this.target = { ...STATES.idle };
    this.current = { ...STATES.idle };

    this.rawLevel = 0;            // pushed by app.js each frame
    this.smoothLevel = 0;         // attack fast / release slow, as the old orb did

    this.clock = new THREE.Clock();
    this.group = null;
    this.points = null;
    this.nodes = null;
    this.uniforms = null;

    this.initScene();
    this.initMaterials();
    this.loadModel();
    this.renderer.setAnimationLoop(() => this.tick());
  }

  initScene() {
    const w = this.container.clientWidth || 460;
    const h = this.container.clientHeight || 460;

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 100);
    this.camera.position.set(0, 0.05, 1.95);
    this.camera.lookAt(0, 0, 0);

    this.renderer = new THREE.WebGLRenderer({
      alpha: true, antialias: true, powerPreference: "high-performance",
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setSize(w, h, false);
    this.renderer.setClearColor(0x000000, 0);          // pure transparent overlay
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;

    this.container.innerHTML = "";
    this.container.appendChild(this.renderer.domElement);

    this.group = new THREE.Group();
    this.scene.add(this.group);

    new ResizeObserver(() => this.resize()).observe(this.container);
    window.addEventListener("resize", () => this.resize());
  }

  initMaterials() {
    this.uniforms = {
      uTime:       { value: 0 },
      uLevel:      { value: 0 },
      uDisperse:   { value: 0 },
      uContract:   { value: 0 },
      uWave:       { value: 0 },
      uDim:        { value: 1 },
      uOffline:    { value: 0 },
      uPixelRatio: { value: Math.min(window.devicePixelRatio || 1, 2) },
      uCyan:    { value: PALETTE.cyan },
      uIndigo:  { value: PALETTE.indigo },
      uViolet:  { value: PALETTE.violet },
      uMagenta: { value: PALETTE.magenta },
      uEmber:   { value: PALETTE.ember },
    };

    const shared = {
      uniforms: this.uniforms,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    };
    this.pointMaterial = new THREE.ShaderMaterial({
      ...shared, vertexShader: POINT_VERT, fragmentShader: POINT_FRAG,
    });
    this.nodeMaterial = new THREE.ShaderMaterial({
      ...shared, vertexShader: NODE_VERT, fragmentShader: NODE_FRAG, wireframe: true,
    });
  }

  loadModel() {
    new GLTFLoader().load(
      MODEL_URL,
      (gltf) => {
        let positions = null;
        gltf.scene.traverse((o) => {
          if (!positions && o.isMesh && o.geometry?.attributes?.position) {
            positions = o.geometry.attributes.position.array;
          }
        });
        if (positions) this.build(positions);
        else this.build(this.proceduralBrain());
      },
      undefined,
      (err) => {
        // The presence must never be blank — fall back to a procedural brain.
        console.warn("brain.glb failed to load, using procedural geometry", err);
        this.build(this.proceduralBrain());
      }
    );
  }

  /** Dual-hemisphere ellipsoid in the same units as brain.glb. */
  proceduralBrain(count = 2800) {
    const out = new Float32Array(count * 3);
    for (let i = 0; i < count; i++) {
      const hemi = Math.random() > 0.5 ? 1 : -1;
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(2 * Math.random() - 1);
      const r = 0.46 + (Math.random() - 0.5) * 0.1;
      out[i * 3]     = r * Math.sin(phi) * Math.cos(theta) * 0.85 + hemi * 0.2;
      out[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta) * 0.9;
      out[i * 3 + 2] = r * Math.cos(phi) * 0.95;
    }
    return out;
  }

  build(positions) {
    const count = positions.length / 3;

    // Centre the mesh: brain.glb sits slightly off origin.
    const c = new THREE.Vector3();
    for (let i = 0; i < count; i++) c.add(new THREE.Vector3(positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2]));
    c.divideScalar(count);
    const centred = new Float32Array(positions.length);
    for (let i = 0; i < count; i++) {
      centred[i * 3]     = positions[i * 3]     - c.x;
      centred[i * 3 + 1] = positions[i * 3 + 1] - c.y;
      centred[i * 3 + 2] = positions[i * 3 + 2] - c.z;
    }

    // --- Particle layer: every vertex --------------------------------------
    const phases = new Float32Array(count);
    const randoms = new Float32Array(count);
    for (let i = 0; i < count; i++) {
      phases[i] = Math.random() * Math.PI * 2;
      randoms[i] = Math.random();
    }
    const pGeo = new THREE.BufferGeometry();
    pGeo.setAttribute("position", new THREE.BufferAttribute(centred, 3));
    pGeo.setAttribute("aPhase", new THREE.BufferAttribute(phases, 1));
    pGeo.setAttribute("aRandom", new THREE.BufferAttribute(randoms, 1));
    this.points = new THREE.Points(pGeo, this.pointMaterial);
    this.group.add(this.points);

    // --- Node layer: an instanced wireframe box every Nth vertex ----------
    const nodeCount = Math.floor(count / NODE_EVERY);
    const box = new THREE.BoxGeometry(0.004, 0.004, 0.004, 1, 1, 1);
    const rot = new Float32Array(nodeCount);
    const size = new Float32Array(nodeCount);
    const nPhase = new Float32Array(nodeCount);
    for (let i = 0; i < nodeCount; i++) {
      rot[i] = THREE.MathUtils.randFloat(-1, 1);       // as the reference
      size[i] = THREE.MathUtils.randFloat(0.3, 3);     // as the reference
      nPhase[i] = Math.random() * Math.PI * 2;
    }
    box.setAttribute("aRotation", new THREE.InstancedBufferAttribute(rot, 1));
    box.setAttribute("aSize", new THREE.InstancedBufferAttribute(size, 1));
    box.setAttribute("aPhase", new THREE.InstancedBufferAttribute(nPhase, 1));

    this.nodes = new THREE.InstancedMesh(box, this.nodeMaterial, nodeCount);
    const dummy = new THREE.Object3D();
    for (let i = 0; i < nodeCount; i++) {
      const v = i * NODE_EVERY;
      dummy.position.set(centred[v * 3], centred[v * 3 + 1], centred[v * 3 + 2]);
      dummy.updateMatrix();
      this.nodes.setMatrixAt(i, dummy.matrix);
    }
    this.nodes.instanceMatrix.needsUpdate = true;
    this.group.add(this.nodes);
  }

  // ---------- Public API (matches the orb it replaces) ----------

  /** idle | listening | thinking | speaking | offline */
  setState(name) {
    if (!STATES[name] || name === this._state) return;
    this._state = name;
    this.target = { ...STATES[name] };
  }

  get state() { return this._state; }

  /** Live audio envelope 0..1: mic while listening, playback while speaking. */
  setLevel(v) { this.rawLevel = Math.max(0, Math.min(1, v || 0)); }

  /** Alias for the spec's name. */
  setAudioAmplitude(v) { this.setLevel(v); }

  resize() {
    const w = this.container.clientWidth, h = this.container.clientHeight;
    if (!w || !h) return;
    const pr = Math.min(window.devicePixelRatio || 1, 2);
    this.renderer.setPixelRatio(pr);
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.uniforms.uPixelRatio.value = pr;
  }

  tick() {
    const dt = Math.min(this.clock.getDelta(), 0.1);
    const t = this.clock.elapsedTime;
    const u = this.uniforms;

    // Ease every state parameter so changes glide rather than snap.
    const k = 1 - Math.pow(0.004, dt);
    for (const key of Object.keys(this.target)) {
      this.current[key] += (this.target[key] - this.current[key]) * k;
    }

    // Level: attack fast, release slow, like the orb — it drives motion only.
    const lk = this.rawLevel > this.smoothLevel ? 1 - Math.pow(0.002, dt) : 1 - Math.pow(0.35, dt);
    this.smoothLevel += (this.rawLevel - this.smoothLevel) * lk;

    u.uTime.value = t;
    u.uLevel.value = this.smoothLevel;
    u.uDisperse.value = this.current.disperse;
    u.uContract.value = this.current.contract;
    u.uWave.value = this.current.wave;
    u.uDim.value = this.current.dim;
    u.uOffline.value = this.current.offline;

    if (this.group) {
      this.group.rotation.y += dt * (this.current.spin + this.smoothLevel * 0.15);
      this.group.rotation.x = Math.sin(t * 0.5) * 0.08;
    }

    this.renderer.render(this.scene, this.camera);
  }
}

// Mount on the stage the orb used, under the same global, so app.js needs no change.
const stage = document.getElementById("orb-stage");
if (stage) {
  window.FridayOrb = new FridayBrainVisualizer(stage);
}
