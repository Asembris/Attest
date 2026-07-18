import { useEffect, useRef } from 'react';
import * as THREE from 'three';

// The ambient particle field behind the Hero. Ported from the design pass as IMPERATIVE
// three.js (a raw canvas + one useEffect), not react-three-fiber: the field is a custom
// additive-blend shader with a cursor "constellation", click shockwaves and heat propagation
// that the declarative R3F model fights rather than helps. `three` is bundled (never a
// runtime CDN), and this file is lazy-imported by Hero so the heavy chunk stays code-split.
//
// It is presentation only — it renders nothing about the catalog and holds no product state.
// Hero mounts it only when prefers-reduced-motion is not set, so there is no motion to opt
// out of here; the effect still tears down every listener, the RAF, and the GL context.

const ACCENT = '#6E8FBF';
const VIOLET = '#9a8ff0';
const FIELD = 17;
const MAX_DISTANCE = 4.6;

function nodeCountForWidth(w: number): number {
  if (w < 640) return 52;
  if (w < 1024) return 74;
  return 92;
}

export default function HeroLattice() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const accent = new THREE.Color(ACCENT);
    const violet = new THREE.Color(VIOLET);
    const hot = new THREE.Color(0.85, 0.95, 1.25);
    const N = nodeCountForWidth(window.innerWidth);

    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setClearColor(0x000000, 0);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(55, 1, 0.1, 100);
    camera.position.set(0, 0, 12);

    const size = () => {
      const w = canvas.clientWidth || window.innerWidth;
      const h = canvas.clientHeight || window.innerHeight;
      renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.75));
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    };

    // ---- nodes ----
    const pos = new Float32Array(N * 3);
    const vel = new Float32Array(N * 3);
    const energy = new Float32Array(N);
    const baseCol: THREE.Color[] = [];
    for (let i = 0; i < N; i++) {
      pos[i * 3] = (Math.random() - 0.5) * FIELD;
      pos[i * 3 + 1] = (Math.random() - 0.5) * FIELD * 0.68;
      pos[i * 3 + 2] = (Math.random() - 0.5) * FIELD;
      vel[i * 3] = (Math.random() - 0.5) * 0.0045;
      vel[i * 3 + 1] = (Math.random() - 0.5) * 0.0028;
      vel[i * 3 + 2] = (Math.random() - 0.5) * 0.0045;
      baseCol.push(accent.clone().lerp(violet, Math.random() * 0.5));
    }

    const pGeo = new THREE.BufferGeometry();
    const aColor = new Float32Array(N * 3);
    const aSize = new Float32Array(N);
    const aAlpha = new Float32Array(N);
    pGeo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    pGeo.setAttribute('aColor', new THREE.BufferAttribute(aColor, 3));
    pGeo.setAttribute('aSize', new THREE.BufferAttribute(aSize, 1));
    pGeo.setAttribute('aAlpha', new THREE.BufferAttribute(aAlpha, 1));

    const pMat = new THREE.ShaderMaterial({
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      vertexShader: `
        attribute vec3 aColor; attribute float aSize; attribute float aAlpha;
        varying vec3 vColor; varying float vAlpha;
        void main(){ vColor=aColor; vAlpha=aAlpha;
          vec4 mv=modelViewMatrix*vec4(position,1.0);
          gl_PointSize=aSize*(320.0/-mv.z);
          gl_Position=projectionMatrix*mv; }`,
      fragmentShader: `
        varying vec3 vColor; varying float vAlpha;
        void main(){ vec2 c=gl_PointCoord-0.5; float d=length(c);
          float core=smoothstep(0.5,0.0,d); float g=pow(core,1.7);
          gl_FragColor=vec4(vColor, g*vAlpha); }`,
    });
    const points = new THREE.Points(pGeo, pMat);
    scene.add(points);

    // ---- lines ----
    const maxSeg = (N * (N - 1)) / 2;
    const lGeo = new THREE.BufferGeometry();
    const lPos = new Float32Array(maxSeg * 6);
    const lCol = new Float32Array(maxSeg * 6);
    lGeo.setAttribute('position', new THREE.BufferAttribute(lPos, 3));
    lGeo.setAttribute('color', new THREE.BufferAttribute(lCol, 3));
    const lMat = new THREE.LineBasicMaterial({
      vertexColors: true,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    const lines = new THREE.LineSegments(lGeo, lMat);
    scene.add(lines);
    const lineBase = accent.clone().multiplyScalar(0.55);

    // ---- cursor constellation ----
    const CMAX = 14;
    const cGeo = new THREE.BufferGeometry();
    const cPos = new Float32Array(CMAX * 6);
    const cCol = new Float32Array(CMAX * 6);
    cGeo.setAttribute('position', new THREE.BufferAttribute(cPos, 3));
    cGeo.setAttribute('color', new THREE.BufferAttribute(cCol, 3));
    const cMat = new THREE.LineBasicMaterial({
      vertexColors: true,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    const cLines = new THREE.LineSegments(cGeo, cMat);
    scene.add(cLines);

    // ---- shockwave rings ----
    type Ring = { mesh: THREE.Mesh; t: number };
    const rings: Ring[] = [];
    const ringGeo = new THREE.RingGeometry(0.94, 1.0, 96);
    const makeRing = (x: number, y: number, z: number) => {
      const m = new THREE.MeshBasicMaterial({
        color: accent.clone().lerp(hot, 0.4),
        transparent: true,
        opacity: 0.9,
        side: THREE.DoubleSide,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      });
      const mesh = new THREE.Mesh(ringGeo, m);
      mesh.position.set(x, y, z);
      scene.add(mesh);
      rings.push({ mesh, t: 0 });
    };

    // ---- interaction ----
    const mouse = new THREE.Vector2(-10, -10);
    const parallax = new THREE.Vector2(0, 0);
    let lastMove = -9999;
    let hoverStrength = 0;
    type Ripple = { c: THREE.Vector3; t: number };
    const ripples: Ripple[] = [];
    const raycaster = new THREE.Raycaster();
    const plane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);
    const worldAt = (nx: number, ny: number) => {
      raycaster.setFromCamera(new THREE.Vector2(nx, ny), camera);
      const p = new THREE.Vector3();
      raycaster.ray.intersectPlane(plane, p);
      return p ?? new THREE.Vector3();
    };
    const burst = (nx: number, ny: number) => {
      const w = worldAt(nx, ny);
      ripples.push({ c: w.clone(), t: 0 });
      if (ripples.length > 6) ripples.shift();
      makeRing(w.x, w.y, w.z);
    };

    const onMove = (e: PointerEvent) => {
      mouse.x = (e.clientX / window.innerWidth) * 2 - 1;
      mouse.y = -(e.clientY / window.innerHeight) * 2 + 1;
      parallax.set(mouse.x, mouse.y);
      lastMove = performance.now();
    };
    const onDown = (e: PointerEvent) => {
      const nx = (e.clientX / window.innerWidth) * 2 - 1;
      const ny = -(e.clientY / window.innerHeight) * 2 + 1;
      burst(nx, ny);
    };
    window.addEventListener('resize', size);
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerdown', onDown);
    size();

    // ---- loop ----
    const tmp = new THREE.Vector3();
    const scr: THREE.Vector3[] = [];
    for (let i = 0; i < N; i++) scr.push(new THREE.Vector3());
    let last = performance.now();
    let raf = 0;
    let stopped = false;

    const tick = () => {
      if (stopped) return;
      const now = performance.now();
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      const t = now * 0.001;

      const hoverTarget = now - lastMove < 1100 ? 1 : 0;
      hoverStrength += (hoverTarget - hoverStrength) * 0.05;

      camera.position.x += (parallax.x * 1.6 - camera.position.x) * 0.018;
      camera.position.y += (parallax.y * 1.0 - camera.position.y) * 0.018;
      camera.lookAt(0, 0, 0);
      points.rotation.y += 0.00032;
      points.rotation.x += 0.0001;
      lines.rotation.copy(points.rotation);
      points.updateMatrixWorld();

      const RSPEED = 4.0;
      const RLIFE = 4.2;
      for (let r = ripples.length - 1; r >= 0; r--) {
        ripples[r].t += dt;
        if (ripples[r].t > RLIFE) ripples.splice(r, 1);
      }

      const aspect = camera.aspect;
      for (let i = 0; i < N; i++) {
        for (let a = 0; a < 3; a++) {
          pos[i * 3 + a] += vel[i * 3 + a];
          const lim = a === 1 ? FIELD * 0.34 : FIELD * 0.5;
          if (Math.abs(pos[i * 3 + a]) > lim) vel[i * 3 + a] *= -1;
        }
        tmp.set(pos[i * 3], pos[i * 3 + 1], pos[i * 3 + 2]).applyMatrix4(points.matrixWorld);
        scr[i].copy(tmp);
        const p = tmp.clone().project(camera);
        const dx = (p.x - mouse.x) * aspect;
        const dy = p.y - mouse.y;
        const sd = Math.sqrt(dx * dx + dy * dy);
        const ce = Math.exp(-(sd * sd) / 0.12) * hoverStrength;
        energy[i] += (ce * 0.95 - energy[i]) * 0.045;
        for (let r = 0; r < ripples.length; r++) {
          const R = ripples[r].t * RSPEED;
          const wd = scr[i].distanceTo(ripples[r].c);
          const band = wd - R;
          const g = Math.exp(-(band * band) / 2.6);
          if (g > 0.02) {
            energy[i] = Math.min(1.9, energy[i] + g * 0.55 * (1 - ripples[r].t / RLIFE));
          }
        }
        const e = energy[i];
        const pulse = 0.5 + 0.5 * Math.sin(t * 0.7 + i * 0.7);
        const col = baseCol[i].clone().lerp(hot, Math.min(1, e)).multiplyScalar(1 + e * 1.3);
        aColor[i * 3] = col.r;
        aColor[i * 3 + 1] = col.g;
        aColor[i * 3 + 2] = col.b;
        aSize[i] = 0.05 + 0.02 * pulse + e * 0.11;
        aAlpha[i] = 0.45 + 0.18 * pulse + e * 0.9;
      }
      pGeo.attributes.position.needsUpdate = true;
      pGeo.attributes.aColor.needsUpdate = true;
      pGeo.attributes.aSize.needsUpdate = true;
      pGeo.attributes.aAlpha.needsUpdate = true;

      // lines
      let s = 0;
      for (let i = 0; i < N; i++) {
        for (let j = i + 1; j < N; j++) {
          const dx = pos[i * 3] - pos[j * 3];
          const dy = pos[i * 3 + 1] - pos[j * 3 + 1];
          const dz = pos[i * 3 + 2] - pos[j * 3 + 2];
          const d = Math.sqrt(dx * dx + dy * dy + dz * dz);
          if (d < MAX_DISTANCE) {
            const k = s * 6;
            lPos[k] = pos[i * 3];
            lPos[k + 1] = pos[i * 3 + 1];
            lPos[k + 2] = pos[i * 3 + 2];
            lPos[k + 3] = pos[j * 3];
            lPos[k + 4] = pos[j * 3 + 1];
            lPos[k + 5] = pos[j * 3 + 2];
            const df = 1 - d / MAX_DISTANCE;
            const me = Math.max(energy[i], energy[j]);
            const intensity = (0.13 + me * 1.4) * df;
            const lc = lineBase.clone().lerp(hot, Math.min(1, me)).multiplyScalar(intensity);
            lCol[k] = lc.r;
            lCol[k + 1] = lc.g;
            lCol[k + 2] = lc.b;
            lCol[k + 3] = lc.r;
            lCol[k + 4] = lc.g;
            lCol[k + 5] = lc.b;
            s++;
          }
        }
      }
      lGeo.setDrawRange(0, s * 2);
      lGeo.attributes.position.needsUpdate = true;
      lGeo.attributes.color.needsUpdate = true;

      // cursor constellation: link cursor world point to nearest screen nodes
      let cs = 0;
      if (hoverStrength > 0.01) {
        const cw = worldAt(mouse.x, mouse.y);
        const cand: { i: number; d: number }[] = [];
        for (let i = 0; i < N; i++) {
          const p = scr[i].clone().project(camera);
          const dx = (p.x - mouse.x) * aspect;
          const dy = p.y - mouse.y;
          cand.push({ i, d: dx * dx + dy * dy });
        }
        cand.sort((a, b) => a.d - b.d);
        const cc = accent.clone().lerp(hot, 0.55);
        for (let n = 0; n < Math.min(CMAX, cand.length); n++) {
          const idx = cand[n].i;
          const fall = Math.max(0, 1 - cand[n].d / 0.22);
          if (fall <= 0) break;
          const k = cs * 6;
          cPos[k] = cw.x;
          cPos[k + 1] = cw.y;
          cPos[k + 2] = cw.z;
          cPos[k + 3] = scr[idx].x;
          cPos[k + 4] = scr[idx].y;
          cPos[k + 5] = scr[idx].z;
          const v = cc.clone().multiplyScalar((0.5 + fall * 0.9) * hoverStrength);
          cCol[k] = v.r;
          cCol[k + 1] = v.g;
          cCol[k + 2] = v.b;
          cCol[k + 3] = v.r;
          cCol[k + 4] = v.g;
          cCol[k + 5] = v.b;
          cs++;
        }
      }
      cGeo.setDrawRange(0, cs * 2);
      cGeo.attributes.position.needsUpdate = true;
      cGeo.attributes.color.needsUpdate = true;

      // rings
      for (let r = rings.length - 1; r >= 0; r--) {
        const R = rings[r];
        R.t += dt;
        const scale = R.t * RSPEED;
        R.mesh.scale.setScalar(Math.max(0.001, scale));
        R.mesh.quaternion.copy(camera.quaternion);
        (R.mesh.material as THREE.MeshBasicMaterial).opacity = Math.max(0, 0.7 * (1 - R.t / RLIFE));
        if (R.t > RLIFE) {
          scene.remove(R.mesh);
          (R.mesh.material as THREE.MeshBasicMaterial).dispose();
          rings.splice(r, 1);
        }
      }

      renderer.render(scene, camera);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);

    return () => {
      stopped = true;
      cancelAnimationFrame(raf);
      window.removeEventListener('resize', size);
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerdown', onDown);
      for (const r of rings) (r.mesh.material as THREE.MeshBasicMaterial).dispose();
      ringGeo.dispose();
      pGeo.dispose();
      pMat.dispose();
      lGeo.dispose();
      lMat.dispose();
      cGeo.dispose();
      cMat.dispose();
      renderer.dispose();
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', display: 'block' }}
    />
  );
}
