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

## SpeedTree FBX payload

SpeedTree VFX FBX의 메시 payload는 다음 순서를 계약으로 사용한다. 기존 두 UV를
덮어쓰거나 순서를 바꾸지 않는다.

SpeedTree 10.1 batch export preset의 `Options_MA_Fbx.ini`에는
`IncludeVertexBlends=Include`가 반드시 유지되어야 한다. 현재 BWR preset은 이 값으로
확인되었으며, 이를 끄면 아래 `blend_ao` 및 Vertex Color 전달 계약을 보장할 수 없다.

| FBX/Blender index | 이름 | 의미 |
| ---: | --- | --- |
| UV0 | `uv0` | 기존 재질 텍스처 좌표 |
| UV1 | `blend_ao` | U는 SpeedTree blend 값, V는 AO 원본 |

SpeedTree는 Vertex Color RGB와 별도로 AO를 `blend_ao.V`에 내보낸다. BWR은 최종
merged export mesh에서 RGB를 그대로 보존하면서 `blend_ao.V`를 `VertexColor.A`로
복사한다. 따라서 Blender/FBX의 최종 의미는 다음과 같다.

| payload | 의미 |
| --- | --- |
| `VertexColor.R` | leaf 근접도 |
| `VertexColor.G` | trunk/branch height 감쇠 |
| `VertexColor.B` | 기존 값 보존 |
| `VertexColor.A` | `blend_ao.V`에서 복사한 AO |

`VertexColor.G`는 trunk와 일부 branch에만 신호가 있는 **희소 마스크**가 정상이다.
0인 영역을 채우거나 전체 범위로 재정규화하면 의도한 trunk/branch 감쇠가 깨지므로,
높은 zero ratio 자체는 데이터 소실로 판단하지 않는다.

`blend_ao.U`는 모든 loop에서 finite이며 `0..1` 범위여야 한다. BWR validator는 이
범위를 벗어나면 `blend_ao_u_outside_zero_one_nanite_fallback_unsafe`로 Push를
차단한다. 이 조건은 아래 UV2 presence tag의 `1.5` 임계값과 기존 UV fallback이
충돌하지 않도록 보장한다.

## 전달 및 Unreal 소비 계약

| 단계 | 책임 |
| --- | --- |
| SK Vegetation Batch | `tree`로 분류된 SPM에만 위 R authoring을 적용하고 기존 G를 보존 |
| BWR | `blend_ao.V -> VertexColor.A`를 적용하고, TREE에는 UV2 `vertex_color_ga`를 추가해 G/A를 mirror |
| Send2UE | UV0/UV1/UV2와 FBX vertex color를 내보내고 Unreal import의 `VertexColorImportOption.REPLACE`로 전달 |
| Unreal `M_TreeAsset_Master` | UV2 presence tag가 있으면 decoded G를 height mask로 사용하고, 없으면 `VertexColor.G`로 fallback |

UE 5.8 Skeletal Nanite 렌더 경로는 임포트된 Vertex Color stream을 버리므로, 메시
데이터와 일반 Skeletal 경로에서 G가 정상이어도 Nanite에서는 `VertexColor.G`만으로
height 감쇠를 읽을 수 없다. 이를 우회하기 위해 BWR은 기존 UV0/UV1을 보존한 채
세 번째 채널인 UV2 `vertex_color_ga`를 다음과 같이 기록한다.

```text
UV2.U = 2 + VertexColor.G   # presence-tagged height mask, 유효 범위 2..3
UV2.V = 1 - VertexColor.A   # UE FBX V 반전을 상쇄하는 AO transport
```

머티리얼의 height mask 선택은 다음 계약을 따른다.

```text
TaggedU = TextureCoordinate[2].R
HeightMask = (TaggedU > 1.5) ? (TaggedU - 2) : VertexColor.G
```

즉 `U > 1.5`가 새 payload의 presence tag이며, tag가 없으면 기존
`VertexColor.G` 경로를 그대로 사용한다. UV2가 이미 다른 용도로 점유되어 있거나
`vertex_color_ga`가 index 2가 아니면 기존 UV를 덮어쓰거나 재배열하지 않고 Push를
차단한다.

UE 5.8 Skeletal FBX importer는 UV의 V를 `1 - V`로 변환한다. 따라서 Blender/FBX
transport에는 미리 `1 - A`를 기록해 Unreal의 최종 `UV2.V`가 다시 `A`, 즉 AO가
되도록 한다. 현재 머티리얼은 UV2의 U만 소비하며 V/AO는 아직 셰이딩에 연결하지
않지만, 향후 연결할 때 추가 반전 없이 같은 AO 의미를 사용할 수 있다.

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
- `VertexColor_HeightBlend`: 기본값 `true`; True 경로의 mask는 위 UV2 decode/fallback 결과
- `Height`: 기본값 `0.1`
- Material Usage의 `Used with Skeletal Mesh`: 활성화

R 기반 green tint는 기존 `VertexColor.R` 경로를 유지하며, UV2 workaround는 현재
G height 감쇠만 복구한다. 따라서 Skeletal Nanite에서 R도 필요해지면 별도의 payload
확장 계약과 검증이 필요하다.

머티리얼의 root WPO pin은 Material Attributes의 WPO 출력을 받아 **pass-through**하는
연결이 있다. 그러나 현재 master/layer/function에는 `SpeedTreeWind`, camera-facing,
또는 별도 wind WPO 식이 authored되어 있지 않다. 따라서 현재 상태를 “WPO wind
구현”으로 부르지 않으며, Vertex Color 변경은 이 pass-through 연결을 수정하지 않는다.

## 보존과 실패 원자성

- SPM의 R 패치 전후 G의 semantic signature가 정확히 같아야 한다. B/A는 red-only
  패치 범위 밖이며 이 단계에서는 그대로 둔다. 이후 BWR 단계에서만 A를
  `blend_ao.V`로 채운다.
- R에서는 대상 Branch의 `Style`, `Value`, `ProfileSpline`만 바꾼다. 다른 channel,
  다른 generator, 기존 `CompoundParentSpline`을 다시 직렬화하거나 정규화하지 않는다.
- BWR packing은 Vertex Color RGB와 UV0 `uv0`, UV1 `blend_ao`를 byte/float tolerance
  안에서 보존한다. UV2가 없고 기존 UV 수가 정확히 2개일 때만
  `vertex_color_ga`를 append한다. 같은 이름의 UV2에는 idempotent하게 다시 쓸 수 있지만,
  다른 UV2를 덮어쓰거나 채널을 재정렬하지 않는다.
- 정식 Vertex Color 속성 `color`가 존재하면 그것만 source of truth로 사용한다. 해당
  속성의 domain/type이 잘못된 경우 다른 active color로 대체하지 않고 Push를 차단한다.
- 대상 하나라도 필수 Red property가 없거나 ProfileSpline이 지원되지 않는 형태이면
  해당 호출은 원본 text 전체를 반환한다. 일부 Branch만 바뀐 상태를 기록하지 않는다.
- 기본 `backup_spm=true`에서는 ① 단계 시작 전에 `_spm_backups`에 원본을 보관하며,
  이후 material rename/R authoring 중 실패하면 전체 SPM을 복원한다.
- 같은 입력에 다시 적용하면 byte-for-byte 같은 결과여야 한다(idempotent).
- 해석할 수 없는 leaf `ParentGUID`나 잘못된 Branch `GeneratorGUID`는 report warning에
  남는다. 실제 배포 전에 warning을 검토해 topology 누락이 아닌지 확인한다.

## 검증 기준

- SPM: R 대상 목록이 Node `ParentGUID -> GeneratorGUID` 경로와 일치하고 GUID가
  dedupe되어야 한다.
- SpeedTree 실제 geometry export: R에 root `0`, tip `1` 범위가 생기며 G/B와
  UV0 `uv0`, UV1 `blend_ao`는 패치 전후 동일해야 한다.
- Blender/BWR 및 FBX 재검사: evaluated mesh와 재수입 FBX의 R/G 통계가 source와
  일치하고, A는 `blend_ao.V`, UV2.U는 `2 + G`, `1 - UV2.V`는 Blender/FBX 기준
  A와 일치해야 한다. UV1.U는 finite `0..1`이어야 한다.
- Blender Repair 보고서는 최종 Export mesh의 RGBA min/max/mean/zero ratio와
  AO/payload delta, 최종 UV layer 순서를 기록한다.
  TREE에서 color attribute 자체가 없거나 구조/범위가 잘못되면 Push 전에 차단한다.
  G가 전부 0이면 선택적인 height 감쇠가 꺼진 상태로 보고 경고만 남기며, G의 90% 이상이
  0인 희소 마스크도 계약상 가능한 값으로 통과시킨다.
- Unreal graph: UV2.U가 `1.5`보다 크면 `U - 2`, 아니면 `VertexColor.G`를 선택한
  결과가 height blend에 연결되어야 한다. `Height` 기본값은 `0.1`, `Used with
  Skeletal Mesh`는 활성화되어야 한다. Base Color tint alpha는 `VertexColor.R`에
  연결되어야 한다. 해당 `SetMaterialAttributes`는 legacy
  BaseColor root뿐 아니라 Substrate `Front Material` root에서도 도달 가능해야 하며,
  WPO source는 변경되지 않아야 한다.
- Unreal FBX 재임포트에서는 UV2.U presence tag와 G decode를 검증하고, FBX V 반전
  이후의 UV2.V가 VertexColor.A/AO와 일치하는지도 검증한다.
- Unreal 실제 버전에서 material을 recompile하고 compile error가 없어야 한다.
