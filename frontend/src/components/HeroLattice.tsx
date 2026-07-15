import { useRef, useMemo } from 'react';
import { Canvas, useFrame } from '@react-three/fiber';
import * as THREE from 'three';

const NODE_COUNT = 48;
const FIELD_SIZE = 14;
const MAX_DISTANCE = 4.5;

function Lattice() {
  const groupRef = useRef<THREE.Group>(null);
  const linesRef = useRef<THREE.LineSegments>(null);
  const pointsRef = useRef<THREE.Points>(null);

  const { positions, velocities } = useMemo(() => {
    const pos = new Float32Array(NODE_COUNT * 3);
    const vel = new Float32Array(NODE_COUNT * 3);
    for (let i = 0; i < NODE_COUNT; i++) {
      pos[i * 3] = (Math.random() - 0.5) * FIELD_SIZE;
      pos[i * 3 + 1] = (Math.random() - 0.5) * FIELD_SIZE * 0.7;
      pos[i * 3 + 2] = (Math.random() - 0.5) * FIELD_SIZE;
      vel[i * 3] = (Math.random() - 0.5) * 0.008;
      vel[i * 3 + 1] = (Math.random() - 0.5) * 0.005;
      vel[i * 3 + 2] = (Math.random() - 0.5) * 0.008;
    }
    return { positions: pos, velocities: vel };
  }, []);

  const maxLines = (NODE_COUNT * (NODE_COUNT - 1)) / 2;
  const lineGeometry = useMemo(() => {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(maxLines * 6), 3));
    return geo;
  }, [maxLines]);

  const pointGeometry = useMemo(() => {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    return geo;
  }, [positions]);

  useFrame(() => {
    const pos = pointGeometry.attributes.position.array as Float32Array;

    for (let i = 0; i < NODE_COUNT; i++) {
      pos[i * 3] += velocities[i * 3];
      pos[i * 3 + 1] += velocities[i * 3 + 1];
      pos[i * 3 + 2] += velocities[i * 3 + 2];

      for (let axis = 0; axis < 3; axis++) {
        if (Math.abs(pos[i * 3 + axis]) > FIELD_SIZE / 2) {
          velocities[i * 3 + axis] *= -1;
        }
      }
    }
    pointGeometry.attributes.position.needsUpdate = true;

    // Update lines
    const linePos = lineGeometry.attributes.position.array as Float32Array;
    let lineIdx = 0;
    for (let i = 0; i < NODE_COUNT; i++) {
      for (let j = i + 1; j < NODE_COUNT; j++) {
        const dx = pos[i * 3] - pos[j * 3];
        const dy = pos[i * 3 + 1] - pos[j * 3 + 1];
        const dz = pos[i * 3 + 2] - pos[j * 3 + 2];
        const dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
        if (dist < MAX_DISTANCE) {
          linePos[lineIdx * 6] = pos[i * 3];
          linePos[lineIdx * 6 + 1] = pos[i * 3 + 1];
          linePos[lineIdx * 6 + 2] = pos[i * 3 + 2];
          linePos[lineIdx * 6 + 3] = pos[j * 3];
          linePos[lineIdx * 6 + 4] = pos[j * 3 + 1];
          linePos[lineIdx * 6 + 5] = pos[j * 3 + 2];
          lineIdx++;
        }
      }
    }
    lineGeometry.setDrawRange(0, lineIdx * 2);
    lineGeometry.attributes.position.needsUpdate = true;

    if (groupRef.current) {
      groupRef.current.rotation.y += 0.0006;
      groupRef.current.rotation.x += 0.0002;
    }
  });

  return (
    <group ref={groupRef}>
      <points ref={pointsRef}>
        <bufferGeometry attach="geometry" {...pointGeometry} />
        <pointsMaterial
          size={0.06}
          color="#6E8FBF"
          transparent
          opacity={0.55}
          sizeAttenuation
          depthWrite={false}
        />
      </points>
      <lineSegments ref={linesRef}>
        <primitive object={lineGeometry} attach="geometry" />
        <lineBasicMaterial color="#6E8FBF" transparent opacity={0.12} depthWrite={false} />
      </lineSegments>
    </group>
  );
}

export default function HeroLattice() {
  return (
    <Canvas
      camera={{ position: [0, 0, 10], fov: 55 }}
      dpr={[1, 1.5]}
      gl={{ antialias: true, alpha: true }}
      style={{ position: 'absolute', inset: 0, filter: 'blur(1px)' }}
    >
      <ambientLight intensity={0.4} />
      <Lattice />
    </Canvas>
  );
}
