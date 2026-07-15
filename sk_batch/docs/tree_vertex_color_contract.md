# Tree Vertex Color 계약

이 문서는 `SpeedTree SPM -> SK Vegetation Batch -> BWR -> Send2UE -> Unreal`
경로에서 트리 Vertex Color가 가져야 하는 의미를 고정한다. 채널의 의미를 바꾸거나
중간 단계에서 재계산하지 않는다.

## SpeedTree 곡선 해석

SpeedTree의 초록색 **parent curve**는 같은 generator가 만든 node의 값을 그 node가
공유 부모의 어느 위치에서 시작하는지에 따라 바꾼다. 청록색 **profile curve**는
각 개별 node의 root에서 tip까지 속성 적용량을 바꾼다. 이 구분은 SpeedTree의
[Curves Overview](https://docs8.speedtree.com/modeler/doku.php?id=curves_overview)에
명시되어 있다. Generator가 geometry를 만드는 규칙과 generator/node의 구분은
[Modeling approach](https://docs9.speedtree.com/modeler/doku.php?id=kcmodelingapproach)와
[Generation properties](https://docs9.speedtree.com/modeler/doku.php?id=generation_properties)를
기준으로 한다.

이 계약에서 `profile 1 -> 0.5` 또는 `profile 0 -> 1`은 parent 상의 배치 방향이
아니라 **각 branch node 자체의 root -> tip** 방향을 뜻한다.

## G: trunk 강도와 branch 세대 감쇠

G는 기존 트리 구조의 강도/height mask이다. 새 R 계약을 적용해도 이 설정과 최종
값을 그대로 보존해야 한다.

| 대상 | Style | Value | ProfileSpline (root -> tip) | 의미 |
| --- | --- | ---: | --- | --- |
| Tree | 기존 초기값 | `0` | 기존값 | G의 초기 기준값 |
| Trunk | `Set` (`0`) | `1` | `1 -> 0.5` | trunk가 절대 G 필드를 시작 |
| Child Branch | `Offset from parent` (`1`) | `-0.5` | `0 -> 1` | root에서는 부모 접점값을 유지하고 tip으로 갈수록 `0.5` 감쇠 |

Child Branch의 정규화된 길이 위치를 `t`, profile 값을 `p(t)`라고 하면 의도한
관계는 다음과 같다.

```text
G_child(t) = clamp(G_parent(attachment) - 0.5 * p(t), 0, 1)
p(0) = 0, p(1) = 1
```

즉 Trunk는 `Set`으로 절대 기준을 만들고, 각 자식 Branch 세대는 부모의 실제
attachment 값을 이어받은 뒤 tip 방향으로 감쇠한다. Generator 이름, 화면상의 순서,
artist가 붙인 label로 이 계층을 다시 추측하지 않는다.

## R: leaf 근접도

R은 leaf 바로 위 Branch의 root에서 leaf 쪽 tip까지 증가하는 근접도이다. 대상은
저장된 node topology로만 정한다.

1. `Node.Type`에 `leaf`가 포함된 leaf-type Node를 찾는다.
2. 그 Node의 `ParentGUID`를 실제 Node로 해석한다.
3. 부모 Node의 type이 `Branch`일 때만 계속한다.
4. 부모 Branch Node의 `GeneratorGUID`가 가리키는 Branch Generator를 대상으로 한다.
5. 같은 Branch Generator를 여러 leaf Node가 공유하면 `GeneratorGUID`로 dedupe한다.

Generator link, generator 이름, index, 화면상의 인접성은 대상 선정 근거가 아니다.
선정된 Branch Generator에는 다음 값을 기록한다.

| 속성 | 값 |
| --- | --- |
| `Vertex Color:Red:Style` | `Set` (`0`) |
| `Vertex Color:Red:Value` | `1` |
| `ProfileSpline` | `Y = X`, 즉 root `0` -> tip `1` |

기존 `CompoundParentSpline`, variance, leaf Generator의 R, 비대상 Branch의 R은
바꾸지 않는다. 현재 트리 자산의 leaf Generator는 기존 `Offset/0`을 유지해 Branch
attachment의 R을 상속하는 것이 전제이며, 자동 패치는 그 leaf 설정을 덮어쓰지 않는다.

## 전달 및 Unreal 소비 계약

| 단계 | 책임 |
| --- | --- |
| SK Vegetation Batch | `tree`로 분류된 SPM에만 위 R authoring을 적용하고 기존 G를 보존 |
| BWR | SpeedTree에서 나온 RGBA를 Blender evaluated mesh까지 값 변경 없이 유지 |
| Send2UE | FBX vertex color를 내보내고 Unreal import의 `VertexColorImportOption.REPLACE`로 전달 |
| Unreal `M_TreeAsset_Master` | G를 height/displacement blend mask로, R을 green tint mask로 소비 |

Unreal의 green tint는 `SetMaterialAttributes`의 Base Color를 다음과 같이 감싼다.
그 결과는 `Surface Weather Effects`와 Substrate 변환을 거쳐 최종 `Front Material`
셰이딩 경로에 도달한다.

```text
TintAlpha = saturate(VertexColor.R * LeafProximityGreenTintStrength)
BaseColor' = lerp(BaseColor, BaseColor * LeafProximityGreenTint, TintAlpha)
```

현재 parameter 계약은 다음과 같다.

- `Leaf Proximity Green Tint`: 기본값 `(0.75, 1.0, 0.75, 1.0)`
- `Leaf Proximity Green Tint Strength`: 기본값 `0.35`
- `VertexColor_HeightBlend`: mask 입력은 `VertexColor.G`

머티리얼의 root WPO pin은 Material Attributes의 WPO 출력을 받아 **pass-through**하는
연결이 있다. 그러나 현재 master/layer/function에는 `SpeedTreeWind`, camera-facing,
또는 별도 wind WPO 식이 authored되어 있지 않다. 따라서 현재 상태를 “WPO wind
구현”으로 부르지 않으며, Vertex Color 변경은 이 pass-through 연결을 수정하지 않는다.

## 보존과 실패 원자성

- R 패치 전후 G의 semantic signature가 정확히 같아야 한다. B/A는 red-only 패치의
  범위 밖이며 그대로 둔다.
- R에서는 대상 Branch의 `Style`, `Value`, `ProfileSpline`만 바꾼다. 다른 channel,
  다른 generator, 기존 `CompoundParentSpline`을 다시 직렬화하거나 정규화하지 않는다.
- 대상 하나라도 필수 Red property가 없거나 ProfileSpline이 지원되지 않는 형태이면
  해당 호출은 원본 text 전체를 반환한다. 일부 Branch만 바뀐 상태를 기록하지 않는다.
- 기본 `backup_spm=true`에서는 ① 단계 시작 전에 `_spm_backups`에 원본을 보관하며,
  이후 calibration/material rename/R authoring 중 실패하면 전체 SPM을 복원한다.
- 같은 입력에 다시 적용하면 byte-for-byte 같은 결과여야 한다(idempotent).
- 해석할 수 없는 leaf `ParentGUID`나 잘못된 Branch `GeneratorGUID`는 report warning에
  남는다. 실제 배포 전에 warning을 검토해 topology 누락이 아닌지 확인한다.

## 검증 기준

- SPM: R 대상 목록이 Node `ParentGUID -> GeneratorGUID` 경로와 일치하고 GUID가
  dedupe되어야 한다.
- SpeedTree 실제 geometry export: R에 root `0`, tip `1` 범위가 생기며 G/B/A는
  패치 전후 동일해야 한다.
- Blender/BWR 및 FBX 재검사: evaluated mesh와 재수입 FBX의 R/G 통계가 source와
  일치해야 한다.
- Unreal graph: height blend는 `VertexColor.G`, Base Color tint alpha는
  `VertexColor.R`에 연결되어야 한다. 해당 `SetMaterialAttributes`는 legacy
  BaseColor root뿐 아니라 Substrate `Front Material` root에서도 도달 가능해야 하며,
  WPO source는 변경되지 않아야 한다.
- Unreal 실제 버전에서 material을 recompile하고 compile error가 없어야 한다.
