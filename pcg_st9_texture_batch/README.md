# PCG ST9 → SK 전환 준비 보드

언리얼 PCG와 작업 레벨에 직접 배치된 ST9 나무(WPO + 마스크 머티리얼)를
**SK_ 데이터(나나이트 + 논마스크 지오메트리 + 버추얼 텍스처)** 로 바꾸기 위한 상태 보드.

실행:

```bat
PCG_ST9_Texture_Batch.bat
```

## 화면 구성

- **표**: 나무 폴더 하나 = 한 행. 컬럼 순서 = 작업 순서.
- **행 클릭** → 아래 상세 패널에 "이 폴더는 뭐가 되어 있고, 다음에 어느 프로그램에서
  뭘 하면 되는지"가 문장으로 나온다. 파일 저장이나 다른 창 열기 없음.
- **로그**: 실행/건너뜀 내역과 이유.

## 단계 (표의 컬럼)

| 컬럼 | 뜻 | 누가 하나 |
|---|---|---|
| ① SK + 머티리얼 이름 | 원본 SPM을 `SK_이름.spm`으로 복사 + 머티리얼 `M_` 이름 및 공용 이름 정리 | **[① 실행] 버튼이 자동 처리** |
| ② 잎 메시 (Blender) | 아틀라스 리프 제너레이터로 오파시티 없는 잎 지오메트리 생성 | **[② 실행] 버튼이 자동 처리** (헤드리스 Blender) — 직접 할 값도 상세 패널에 나온다 |
| ③ 텍스처 (Substance) | 표시된 SpeedTree Generator가 사용하는 고유 연결 텍스처 세트마다 `T_` 6장 추출 | **[③ 실행] 버튼이 자동 처리** (sbsrender) — SBS 그래프와 SPM 연결도 `T_`로 관리 |

SK SPM 리페어 `.blend`의 상태 확인과 교체는 `..\sk_batch\SK_Batch.bat`가 단독으로 담당한다.
이 보드는 해당 상태를 판정하거나 작업 완료 여부에 포함하지 않는다.

## [① 실행] — SK 만들기 + M_ 이름 붙이기

- 체크된 행 중 ①이 필요한 항목에만 적용된다.
- 수정 전 원본은 각 폴더의 `_spm_backups\` 에 백업이 남는다.
- 실행 전 확인창에 "SK 몇 개 생성, 이름 몇 개 변경"이 정확히 표시된다.
- 문제가 있는 항목은 자동으로 건너뛰고 로그에 이유를 적는다:
  - **중복 매칭**: 같은 PCG 메시가 여러 폴더에 매칭됨 → 어느 폴더가 진짜인지 먼저 확인
  - **원본 못 찾음**: 폴더에서 해당 메시의 원본 SPM을 못 찾음 → 파일 이름 확인
  - **기본 이름 머티리얼**(`Material 2` 등): 이름은 그대로 두고 나머지만 처리(부분 적용).
    SpeedTree에서 이름을 지은 뒤 다시 ①을 누르면 마저 처리된다.
- 표에는 `SK_x.spm → Material 2`처럼 문제 SPM과 이름을 함께 표시한다. 여러 SPM이면
  `SK_x.spm(3), SK_y.spm(3)`으로 위치를 요약하고, 상세 패널에서 전체 경로와 이름을 본다.
- `⚠ 문제 표시된 항목도 적용` 체크박스를 켜면 중복 매칭·기본 이름도 강제 적용된다.
- 여러 나무가 같은 공용 bark-end 세트를 쓰면서 이름만 달랐던
  `M_bark_common_locast_end_01`, `M_bark_common_dogWood_end_01` 등은
  `M_bark_common_end_01`로 통일한다. `stem ..._dead`처럼 인스턴스 틴트 차이를
  나타내는 이름은 통합하지 않는다.

## 아틀라스 항목과 ② 원본 잎 메시가 잡히는 경로

1. **클러스터 SPM**: `Cluster\*.spm` → `M_{이름}_atlas_01` (elm 방식)
2. **머티리얼 이름**: 클러스터 없이 SK SPM의 머티리얼이 아틀라스를 직접 쓰는 경우
   (anamone 방식). SK SPM 머티리얼 이름이 ⓐ atlas 폴더의 blend 이름과 일치,
   ⓑ SBS의 `T_` 그래프(또는 레거시 `M_` 그래프) 이름과 일치,
   ⓒ `..._atlas_NN` 패턴, 또는 ⓐⓑ에
   `_green/_stem` 같은 Auto Split 그룹 접미사가 붙은 형태면 아틀라스로 감지한다.
   bark/decal/stem 계열은 ③(텍스처)만 추적하고 ②(잎 메시) 대상에서 뺀다.

②는 Cluster 결과 TGA 자체를 메시화하지 않는다. 최종 SPM이
`Cluster\leaf_x_01.tga`를 쓰면 같은 이름의 `Cluster\leaf_x_01.spm`을 열어,
그 안의 잎 머티리얼이 참조하는 **원본 잎 알베도+알파 아틀라스**를 찾는다.
클러스터 없이 원본 잎 아틀라스를 직접 쓰는 SPM도 같은 방식으로 찾는다.
동일한 절대경로의 알베도+알파 쌍을 여러 Cluster SPM이 재사용하면 blend/메시는
한 번만 만들고, 그 Cluster 결과를 사용하던 **최종 SK SPM 목록**만 합친다.
Cluster SPM은 원본 아틀라스 추적 근거일 뿐이며 대상 개수와 파일 수정에서 제외한다.
최종 SPM의 옛 Cluster Generator도 같은 원칙이다. 공용 Legacy Cluster 계약의
receipt에 기록된 Generator GUID는 숨김/표시 여부와 무관하게 과거 출처로만 보존한다.
receipt가 없는 현재 Generator만 실제 export 참여 여부를 보고 ② 작업 대상으로 센다.
또한 현재 Material+Mesh 연결과 같은 이름의 Blender 아틀라스가 이미 완성돼 있으면
예전 원본 텍스처가 남아 있어도 재제작 작업으로 되돌리지 않는다.

- 다른 폴더의 아틀라스를 쓰는 경우(densiflora→scotspine)는 **그래프가 있는
  폴더 소유**로 정리된다: 작업은 소유 폴더 행에 나오고, 사용하는 폴더에는
  "공유 — 그쪽 행에서 처리"로 표시된다.
- 출력 텍스처는 sbs 옆, `texture\`, `texture\substance\` 를 모두 뒤져서 찾는다.

## [② 실행] — 잎 메시 blend 만들기 (헤드리스 Blender)

- blend가 없는 고유 원본 잎 아틀라스 묶음마다 `jobs\atlas_blend_job.py` 를 `--factory-startup` 백그라운드
  Blender로 돌린다. 사용자 시작 애드온은 로드하지 않고 필요한
  `atlas_leaf_mesh_builder`만 스크립트에서 직접 활성화한다.
- 최종 SPM → 참조 Cluster SPM → 내부 잎 머티리얼의 원본 알베도/알파 순으로
  실제 참조 체인을 읽는다. Cluster 결과 TGA는 ② 입력으로 쓰지 않는다.
- pair 목록을 비워서 넘기므로 **감지된 모든 알파 아일랜드**가 잎 메시가 된다.
  Quality=SPEEDTREE_LOW, Plate=One Plate 고정. `atlas\M_이름.blend` 로 저장.
- 기본은 blend 생성까지만. `최종 SK에 잎 메시 머티리얼 생성` 체크박스를 켜면
  Build/Update Target SPMs까지 실행한다. `Material_v8`과 FBX/XML `Mesh` 자산을
  최종 SK SPM에 등록하지만 Leaf Mesh Generator 슬롯에는 자동 연결하지 않는다.
  Cluster SPM은 수정하지 않는다. 등록 직전 각 최종 SK SPM은 `_spm_backups\`에
  백업되고, 작업 실패 시 자동 복원된다. 최종 SK가 바뀌면 SK Batch의
  ② Blender Repair에서 리페어 `.blend`를 교체한다.
  메시를 눈으로 먼저 보고 싶으면 끄고, blend 열어 확인한 뒤 애드온에서 직접 반영.

## [③ 실행] — 연결 텍스처 세트별 T_ 텍스처 6장 만들기 (sbsrender)

- 일반 `③ 실행`은 누락·불완전한 세트만 처리한다. `Cluster_System_01.sbsar`를
  수정해 완성된 결과까지 갱신해야 할 때는 별도 `③ 전체 다시 뽑기` 버튼을 사용한다.
  이 수동 실행은 체크 여부와 무관하게 현재 표의 완성 세트도 세트당 6장 전부 다시 렌더하며, 절차형
  SBS 그래프의 기존 쿡 캐시도 재사용하지 않고 현재 Cluster_System으로 다시 쿡한다.
  자동 변경 감지는 하지 않으며, 모든 렌더가 성공하면 결과 전체를 모아 Unreal 동기화를
  한 번만 실행한다. SPM 연결 정리는 이 전용 버튼에서 실행하지 않는다.
- 일반 관리 그래프는 세트 .sbs 전체를 쿡하지 않고 **Cluster_System_01.sbsar를
  sbsrender로 직접 렌더**한다. Cluster_System이 없는 procedural/다중 합성 그래프는
  최종 `basecolor/normal/roughness/height/AO/subsurface/opacity` 노드를 Cluster_System 입력에
  연결해 정규화한 뒤, 해당 SBS 그래프 자체를 cook/render한다. 이미 패킹된 `color/extra`를
  입력으로 되먹이지 않으며 6개 출력은 한 번의 렌더 트랜잭션으로 만든다.
- 아틀라스만 세지 않는다. 최종 SK의 **표시된** SpeedTree Generator가 실제 참조하는
  bark/stem/surface/leaf 등의 연결 텍스처를 모두 처리한다. 숨김 Generator와 숨김 부모
  아래 항목, 미사용 테스트 머티리얼은 생성 대상에서 제외한다.
- `*_yellow`, `*_green`, `*_stem`처럼 SpeedTree Auto Split이 만든 파생 슬롯이 같은
  원본 텍스처 연결을 공유하면 별도 머티리얼 개수로 세지 않고 한 세트로 합친다. 각
  슬롯 이름은 alias로 보존해 같은 suffix 없는 `T_` 6장에 연결한다. 이름이 비슷해도
  실제 원본 연결이 다르면 서로 다른 세트로 유지한다.
- 각 고유 세트의 완료 계약은 항상
  `color/normal/extra/height/opacity/subsurface` 6장이다. 직접 제작 SBS 그래프가 일부
  입력만 제공하면 실제 내부 연결 또는 중립 입력으로 Cluster_System 슬롯을 완성하고,
  6장의 존재·크기·해상도와 그래프의 Cluster 연결을 확인한 뒤에만 완료 처리한다.
- 표는 `머티리얼 N개` 대신 `연결 텍스처 N세트 · 완료/전체 장수`를 표시한다. 6장이
  모두 있고 SPM 연결까지 맞으면 생성 동작은 끝나고 ③ 버튼은 `Unreal 동기화`로 바뀐다.
  파일은 있고 슬롯 연결만 예전 이름이면 `연결 정리` 작업으로 구분한다.
- 이름 계약은 `M_머티리얼 → T_SBS그래프 → T_텍스처 6장`이다. 기존 SBS의 `M_` 그래프는
  수정 전 백업한 뒤 `T_`로 이름을 바꾸고, 새 그래프도 `T_`로 삽입한다.
- 같은 대상의 절차형/직접 제작 `M_` 그래프와 단순 비트맵 `T_` 그래프가 함께 있으면,
  단순 `T_` 복제본을 제거하고 원본 제작 그래프 자체를 `T_`로 승격한다. SBS 노이즈·블렌드·HBAO
  같은 제작 노드를 원본 비트맵 연결로 우회하지 않는다. 원본 그래프에 Opacity 출력이 없으면
  그래프 내부의 실제 Opacity/Alpha 리소스를 별도 출력에 사용한다.
- 복제 과정에서 내부 UID 참조가 깨진 `T_` 그래프는 같은 SBS 안의 정상 authoring 그래프를
  전체 UID 재발급 방식으로 복제해 복구한다. 원본 authoring 그래프는 삭제하거나 개명하지
  않으며, SBS 하나에 여러 대상 그래프가 있으면 파일 단위로 함께 백업·정규화한다.
- 그래프가 있으면 SBS의 비트맵 연결·인스턴스 파라미터를 정본으로 읽어 그대로 사용한다.
  그래프가 없으면 해당 `Material_v8`의 실제 Color/Opacity 등 원본 슬롯만 읽어 자동 연결한다.
  파일명에 `color`가 없다는 이유로 추정하지 않으며, SpeedTree의 Color 슬롯을 근거로 삼는다.
- SpeedTree 원본과 SBS의 Unreal용 출력은 구분한다. 기존
  `M_*_color/normal/extra/height/opacity/subsurface`와 새 `T_*` 출력은 원본 입력 후보에서 제외한다.
  새 `T_` 6장 렌더가 모두 성공한 뒤 대응하는 기존 `M_` 출력은 자동 삭제한다.
  로컬 PNG가 외부 원본과 픽셀 단위로 동일하면 외부 원본 경로로 정규화한다.
- 공용 SBS는 나무 폴더뿐 아니라 `source_texture_roots` 아래에서도 찾아 한 그래프/한 출력으로
  공유한다. AO가 없으면 Designer `hbao_2.sbs`로 height에서 HBAO를 만들고,
  SDF(distance)=0, 노멀 OpenGL/DirectX는 원본 출처로 판정한다.
- 현재 Cluster_System_01.sbsar는 OpenGL 입력을 DirectX로 바꾸지만 `normal` 토글을 CLI에
  노출하지 않는다. TCom·Megascan은 그대로 렌더하고, 이미 DirectX인 Substance 원본은
  출력 normal의 G 채널을 한 번 더 보정해 최종 DirectX를 유지한다.
- 원본 가로세로 비율을 유지하고 장축만 최대 4K로 제한한 TGA로 SBS 옆 texture 폴더에
  `T_이름_color/normal/extra/height/opacity/subsurface.tga`로 저장한다.
  예를 들어 원본 1024×8192는 512×4096으로 읽고 출력하며, 1:1로 강제하지 않는다.
- SpeedTree에는 출력 파일을 다시 가공하지 않고 슬롯 채널 선택으로 연결한다.
  `extra.R → AO`, `extra.G → Gloss + Invert`, `height → 별도 Height`이며,
  Opacity는 경로만 유지하고 비활성화한다. Subsurface/Translucency 원본이 있는 leaf·stem은
  `SubsurfaceColor`를 활성화하고, bark나 해당 출력이 없는 procedural 그래프는 비활성화한다.
  비활성 Subsurface는 `SubsurfaceColor RGB=0`, `SubsurfaceAmount=0`으로 두고,
  활성 Subsurface는 Amount=1로 둔다. Opacity 스칼라는 비활성 상태에서도 1을 유지한다.
  SBS의 `Subsurface_Amount` 입력은 기본적으로 흰색(1)을 쓰되, procedural 그래프가 별도의
  `translucency` 출력을 제공하면 그 값을 Amount에 연결하고 `scatteringcolor`를 Subsurface에 연결한다.
- 렌더는 별도 staging 폴더에서 6장을 완성한 뒤 기존 TGA와 SHA-256을 비교한다. 동일한
  파일은 경로·내용·mtime을 전혀 건드리지 않는다. 실제 변경 파일만 교체하고 기존 변경분만
  `_pcgtex_backups\T_x_타임스탬프\`에 백업하며, 중간 실패 시 전체를 복원한다.
- 생성/확인 후 canonical `T_` 6장은 `/Game/Textures`로 자동 동기화한다. 실행 중인
  MyProject2 에디터가 있으면 Remote Python을 사용하고, 에디터가 꺼져 있으면
  `UnrealEditor-Cmd` 헤드리스 배치를 사용한다. 에디터는 켜져 있지만 Remote가 연결되지
  않으면 동일 `.uasset`의 동시 저장을 피하기 위해 동기화를 보류한다.
- Unreal의 `AssetImportData.FileMD5`가 TGA MD5와 같고 설정도 맞으면 checkout/import/save를
  모두 생략한다. 신규만 Perforce `add`, 변경된 기존 에셋만 checkout/reimport하고 이번 실행이
  소유한 checkout에만 `revert unchanged`를 적용한다. Virtual Texture Streaming을 켜고
  Max Texture Size는 0(제한 없음)으로 유지한다.

설정(`pcg_texture_config.json`): `unreal_levels`, `blender_exe`, `designer_dir`(sbsrender 위치),
`cluster_sbsar`, `cluster_sbsar_normal_behavior`, `atlas_job_timeout`, `sbsrender_timeout`,
`unreal_editor_cmd`, `unreal_texture_sync_enabled`, `unreal_texture_commandlet_fallback`,
`unreal_texture_destination`.

## PCG + 작업 레벨 대상 목록

- **Unreal에서 읽기**: 에디터가 켜져 있을 때. PCG_01 데이터에셋과
  `pcg_texture_config.json`의 `unreal_levels`에 지정된 레벨을 읽어 `pcg_targets.json` 갱신.
  현재 기본 작업 레벨은 `/Game/Level/Cliff_final_01`이다. 레벨은 현재 열려 있지 않아도
  World asset을 읽기 전용으로 로드하므로 사용자의 현재 레벨을 전환하지 않는다.
- **저장된 리포트에서 읽기**: 에디터가 꺼져 있을 때, 저장해 둔 PCG 덤프에서 읽음.
- **PCG/레벨에서 쓰는 폴더만 보기**: 켜면 두 출처 중 하나 이상에 매칭되는 폴더만 나온다.
- 표의 `PCG/레벨 사용` 컬럼은 두 위치에서 사용하는 메시 개수를 읽기 쉽게 표시한다.
- 매칭 안 된 사용 메시와 중복 매칭은 검사 직후 로그에 표시된다.

## CLI (자동화/리포트가 필요할 때만)

GUI에서는 CSV/JSON 리포트를 자동 저장하지 않는다. 산출물이 필요하면 기존 스크립트를 직접 실행:

```bat
python pcg_texture_audit.py --json reports\audit.json --csv reports\audit.csv
python export_all_queues.py --pcg-targets pcg_targets.json --out-dir reports --prefix pcg01 --no-stamp
```

개별 산출물: `export_prepare_plan.py`(SK/M_ 변경 예정 목록), `export_prepare_apply_queue.py`
(`--apply`로 안전 항목 일괄 적용), `export_texture_plan.py`(②③ 작업표),
`export_atlas_handoff_queue.py`(Blender 핸드오프),
`export_sbs_handoff_queue.py`(Substance 핸드오프), `export_review_queue.py`/`export_review_brief.py`
(수동 확인 목록). 모두 읽기 전용이며 GUI와 같은 검사 엔진(`pcg_texture_audit.py`)을 쓴다.
