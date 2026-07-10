# PCG ST9 → SK 전환 준비 보드

언리얼 PCG에서 쓰는 ST9 나무(WPO + 마스크 머티리얼)를
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
| ① SK + M_ 이름 | 원본 SPM을 `SK_이름.spm`으로 복사 + 머티리얼 이름에 `M_` 붙이기 (send2ue 임포트 규칙) | **[① 실행] 버튼이 자동 처리** |
| ② 잎 메시 (Blender) | 아틀라스 리프 제너레이터로 오파시티 없는 잎 지오메트리 생성 | **[② 실행] 버튼이 자동 처리** (헤드리스 Blender) — 직접 할 값도 상세 패널에 나온다 |
| ③ 텍스처 (Substance) | 원본을 Cluster_System_01에 연결해 5장(color/normal/extra/height/opacity) 추출 | **[③ 실행] 버튼이 자동 처리** (sbsrender) — SBS에 M_ 그래프도 넣어준다 |
| ④ SK Blend (SK Batch) | SK SPM의 리페어 `.blend` | 별도 도구 `..\sk_batch\SK_Batch.bat` — 여기서는 상태만 표시 |

## [① 실행] — SK 만들기 + M_ 이름 붙이기

- 체크된 행 중 ①이 필요한 항목에만 적용된다.
- 수정 전 원본은 각 폴더의 `_spm_backups\` 에 백업이 남는다.
- 실행 전 확인창에 "SK 몇 개 생성, 이름 몇 개 변경"이 정확히 표시된다.
- 문제가 있는 항목은 자동으로 건너뛰고 로그에 이유를 적는다:
  - **중복 매칭**: 같은 PCG 메시가 여러 폴더에 매칭됨 → 어느 폴더가 진짜인지 먼저 확인
  - **원본 못 찾음**: 폴더에서 해당 메시의 원본 SPM을 못 찾음 → 파일 이름 확인
  - **기본 이름 머티리얼**(`Material 2` 등): 이름은 그대로 두고 나머지만 처리(부분 적용).
    SpeedTree에서 이름을 지은 뒤 다시 ①을 누르면 마저 처리된다.
- `⚠ 문제 표시된 항목도 적용` 체크박스를 켜면 중복 매칭·기본 이름도 강제 적용된다.

## 아틀라스 항목이 잡히는 두 가지 경로

1. **클러스터 SPM**: `Cluster\*.spm` → `M_{이름}_atlas_01` (elm 방식)
2. **머티리얼 이름**: 클러스터 없이 SK SPM의 머티리얼이 아틀라스를 직접 쓰는 경우
   (anamone 방식). SK SPM 머티리얼 이름이 ⓐ atlas 폴더의 blend 이름과 일치,
   ⓑ SBS의 M_ 그래프 이름과 일치, ⓒ `..._atlas_NN` 패턴, 또는 ⓐⓑ에
   `_green/_stem` 같은 Auto Split 그룹 접미사가 붙은 형태면 아틀라스로 감지한다.
   bark/decal/stem 계열은 ③(텍스처)만 추적하고 ②(잎 메시) 대상에서 뺀다.

- 다른 폴더의 아틀라스를 쓰는 경우(densiflora→scotspine)는 **그래프가 있는
  폴더 소유**로 정리된다: 작업은 소유 폴더 행에 나오고, 사용하는 폴더에는
  "공유 — 그쪽 행에서 처리"로 표시된다.
- 출력 텍스처는 sbs 옆, `texture\`, `texture\substance\` 를 모두 뒤져서 찾는다.

## [② 실행] — 잎 메시 blend 만들기 (헤드리스 Blender)

- blend가 없는 원본 묶음마다 `jobs\atlas_blend_job.py` 를 `--factory-startup` 백그라운드
  Blender로 돌린다. 사용자 시작 애드온은 로드하지 않고 필요한
  `atlas_leaf_mesh_builder`만 스크립트에서 직접 활성화한다.
- 알베도/알파 우선순위: **그 아틀라스의 렌더 결과물(`M_x_color/opacity.tga` — SPM이
  실제로 쓰는 텍스처)** → SBS M_ 그래프의 알베도/오파시티 연결 → SPM 원본 참조 추측.
  셋 다 없으면 건너뛰고 "③으로 텍스처를 먼저 만들면 그걸 사용"이라고 알려준다.
  (확인창에 파일명 표시 — 틀리면 취소.)
- pair 목록을 비워서 넘기므로 **감지된 모든 알파 아일랜드**가 잎 메시가 된다.
  Quality=SPEEDTREE_LOW, Plate=One Plate 고정. `atlas\M_이름.blend` 로 저장.
- 기본은 blend 생성까지만. `만든 뒤 SK SPM에 잎 메시 반영` 체크박스를 켜면
  Build/Update Target SPMs 까지 실행한다 (SK SPM 수정 → ④ 재생성 필요).
  반영 직전 각 SK SPM은 `_spm_backups\`에 백업되고, 작업 실패 시 자동 복원된다.
  메시를 눈으로 먼저 보고 싶으면 끄고, blend 열어 확인한 뒤 애드온에서 직접 반영.

## [③ 실행] — 아틀라스 텍스처 5장 만들기 (sbsrender)

- 세트 .sbs 전체를 sbscooker로 쿡하지 않는다(레거시 그래프의 깨진 참조 + Cluster_System_01
  이중 의존성 때문에 Error 13). 대신 **Cluster_System_01.sbsar를 sbsrender로 직접 렌더**한다.
- SBS에 M_ 그래프가 이미 있으면: 그 그래프의 비트맵 연결·인스턴스 파라미터를 XML에서
  읽어 그대로 사용한다. M_ 그래프는 "비트맵→인스턴스→출력" 순수 통과 구조라 결과가
  Designer 수동 익스포트와 같다 (elm에서 픽셀 비교 검증: color max 2, normal 완전 일치).
- 없으면: 같은 폴더·파일명 계열의 알베도/알파/노멀/height를 **하나의 원본 세트**로
  묶어서 선택한다. 머티리얼 기반 아틀라스는 해당 머티리얼이 실제로 들어 있는 SPM의
  `Material_v8 → TexFilename` 연결만 읽으므로, 같은 폴더의 다른 나무/풀 원본이 섞이지 않는다.
  동률 후보가 둘 이상이면 서로 다른 세트를 섞지 않고 해당 항목을
  건너뛰어 로그에 후보를 표시한다. 표에서 행을 고른 뒤 **원본 세트 지정 (선택 행)** 버튼으로
  실제 세트를 선택하면 `pcg_texture_state.json`에 저장되어 이후 ②/③ 실행에서 재사용된다.
  선택된 원본으로 렌더하고, 이후 Designer에서 관리할 수 있게
  **elm 템플릿을 복제한 M_ 그래프를 .sbs에 삽입**한다 (수정 전 `pcgtex_backup` 백업,
  삽입 실패 시 자동 복원). AO가 없으면 설치된 Designer의 `hbao_2.sbs`로 height에서
  실제 HBAO를 생성해 `_pcgtex_generated\`에 두고 그래프에 연결한다. SDF(distance)=0,
  노멀 OpenGL/DirectX는 원본 출처(TCom·Megascan=OpenGL / sbsar=DirectX)로 판정.
- 현재 Cluster_System_01.sbsar는 OpenGL 입력을 DirectX로 바꾸지만 `normal` 토글을 CLI에
  노출하지 않는다. TCom·Megascan은 그대로 렌더하고, 이미 DirectX인 Substance 원본은
  출력 normal의 G 채널을 한 번 더 보정해 최종 DirectX를 유지한다.
- 4K(`$outputsize=12`) tga로 SBS 옆 texture 폴더에 저장.
- 렌더는 5장을 한 세트로 다시 만든다. 기존 동명 TGA는 먼저
  `_pcgtex_backups\M_x_타임스탬프\`에 백업하며, 실패하면 기존 파일을 복원한다.

설정(`pcg_texture_config.json`): `blender_exe`, `designer_dir`(sbsrender 위치),
`cluster_sbsar`, `cluster_sbsar_normal_behavior`, `atlas_job_timeout`, `sbsrender_timeout`.

## PCG 대상 목록

- **Unreal에서 읽기**: 에디터가 켜져 있을 때. PCG_01 데이터에셋에서 실제 사용하는
  메시 목록을 읽어 `pcg_targets.json` 갱신.
- **저장된 리포트에서 읽기**: 에디터가 꺼져 있을 때, 저장해 둔 PCG 덤프에서 읽음.
- **PCG에서 쓰는 폴더만 보기**: 켜면 매칭되는 폴더만 표에 나온다.
- 매칭 안 된 PCG 메시, 중복 매칭은 검사 직후 로그에 표시된다.

## CLI (자동화/리포트가 필요할 때만)

GUI에서는 CSV/JSON 리포트를 자동 저장하지 않는다. 산출물이 필요하면 기존 스크립트를 직접 실행:

```bat
python pcg_texture_audit.py --json reports\audit.json --csv reports\audit.csv
python export_all_queues.py --pcg-targets pcg_targets.json --out-dir reports --prefix pcg01 --no-stamp
```

개별 산출물: `export_prepare_plan.py`(SK/M_ 변경 예정 목록), `export_prepare_apply_queue.py`
(`--apply`로 안전 항목 일괄 적용), `export_blend_queue.py`(SK Blend 누락/오래됨),
`export_texture_plan.py`(②③ 작업표), `export_atlas_handoff_queue.py`(Blender 핸드오프),
`export_sbs_handoff_queue.py`(Substance 핸드오프), `export_review_queue.py`/`export_review_brief.py`
(수동 확인 목록). 모두 읽기 전용이며 GUI와 같은 검사 엔진(`pcg_texture_audit.py`)을 쓴다.
