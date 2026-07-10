# SK Vegetation Batch (SPM → Blender → Unreal)

WPO/마스크 방식 식생을 스켈레탈 메시(SK_*.spm)로 교체하는 반복 작업을
단계별 버튼으로 자동화하는 도구. `SK_Batch.bat` 더블클릭으로 실행.

## 사용 순서 (빠른 것 → 느린 것)

**🔍 검사 (수정 없음)** — 표에 상태만 채운다:
SPM 본 세팅 상태(미보정/Relative/무본/M_필요), blend 최신 여부,
push에 필요한 핸드오프 산출물(wind JSON) 준비 여부.
오래 걸리는 단계를 돌리기 전에 항상 먼저 눌러보는 용도.

**① SPM 본 세팅 (파일당 ~32~48초, 여러 개 동시 실행)** — SPM만 수정:
- **본 캘리브레이션 (총 본 수 예산 방식)**: SpeedTree의 Relative 본 스타일은
  spline 길이에 비례해 본을 넣으므로, 값이 낮으면 짧은 잔가지는 자동으로
  0본이 된다(원하는 동작). 문제는 "가지당 평균 N본"을 목표로 잡으면 가지가
  수만 개인 큰 나무가 폭증한다는 것(elm_03: 15,234가지 × 3 ≈ 45k목표 →
  80k본). 그래서 **총 본 수 예산**으로 하나의 Relative 값을 맞춘다:
  1. 프로브: 대상 제너레이터를 잠시 Absolute/1로 바꿔 XML 익스포트 —
     Absolute/1 = "가지당 본 정확히 1개"라서 총 본 수 = **총 가지 수**
  2. 예산: `min(가지 수 × 가지당 목표, 최대 총 본 수)`
     · 작은 식물: 가지가 적어 상한 안 걸림 → 가지당 목표대로
     · 큰 나무: 상한이 걸림 → 짧은 잔가지 0~1본, 굵고 긴 가지에만 본 배분
  3. 전 대상 제너레이터에 **동일한** Relative 값을 주고, 총 본 수가 예산에
     맞도록 그 값을 조정(감쇠 비례, 최대 4라운드)
  - Absolute+Bones=0 제너레이터는 "의도적 무본"으로 스킵
  - 제너레이터별 개별 값이 아니라 하나의 값이라 **중복 제너레이터 이름**
    (elm_03의 'Bifurcating'×3 등)에 안전하고, **Size scalar도 자동 반영**
    (Relative가 실제 spline 길이 비례)
  - 실측 예: 소형 풀 anamone 273본(가지당 2), 대형 elm_03 15,234가지 →
    80,000본에서 **1,776본**으로 (잔가지 0, Big 계열 큰 가지에 집중)
- 머티리얼 이름에 `M_` 프리픽스 (attr-only, FBX 전파 검증됨)
- 수정 전 `_spm_backups\`에 백업, 실패 시 자동 복원
- **동시 실행**: SpeedTree 익스포트는 파일 크기와 무관하게 프로세스 기동에
  ~16초가 걸린다(콜드 스타트 지배). 이 시간이 코어별로 병렬화되므로 여러
  파일을 동시에 돌리면 거의 배수로 빨라진다(4개 ≈ 3.8배 실측). "동시 실행"
  옵션으로 개수 조절. 이 단계만 병렬이고 ②③은 순차.

**② Blender Repair (느림, 파일당 수분~수십분)** — 헤드리스 Blender로
BWR `SpeedTree → Import → Repair` 실행, wind 프리셋은 파일명 기반 자동
(tree/bush/weed·grass, deadleaves·deadbranches=무바람), SPM 옆에 같은 이름
`.blend` + wind JSON 저장. **이미 SPM보다 최신인 blend는 건너뛴다**
("완료된 항목도 다시 실행"으로 강제 가능). 재실행 = 갱신.

**③ Unreal Push** — 시작 전에 **준비 검사부터 전부** 수행:
blend 존재+최신, wind JSON 존재, 언리얼 에디터 실행 여부. 준비 안 된 항목은
이유를 표에 남기고 건너뛰고, 준비된 것만 헤드리스 send2ue로 push한다
(임포트 시 머티리얼 파이프라인이 wind JSON 연결까지 자동 수행 + 디스크 저장).

## 옵션 설명 (GUI 툴팁과 동일)

- **가지당 목표 본 수** — 작은 식물의 목표. 총 본 수를 대략 `가지 수 × 이 값`으로
  맞춘다. Relative 값 자체는 파일마다 다르게 계산되는 것이 정상(길이 비례 계수).
- **최대 총 본 수** — 한 나무의 총 본 수 상한. 본 폭증 방지의 핵심. 큰 나무는
  이 상한이 걸려 잔가지가 자동으로 0~1본, 큰 가지에만 본이 배분된다.
- **우선순위 / CPU 코어** — 백그라운드 프로세스의 CPU 사용 제한.
  자식(SpeedTree CLI/Blender)에 상속. 헤드리스 Blender는 GPU를 쓰지 않는다.
- **완료된 항목도 다시 실행** — ②에서 최신 blend가 있어도 다시 만든다.

## 주의

- Wind 열 더블클릭으로 파일별 프리셋 수동 지정 (auto ↔ TREE/BUSH/GRASS/NONE)
- 백업은 각 폴더의 `_spm_backups\` 하위 폴더에만 쌓인다 (작업 폴더 오염 없음)
- 기존 UE 에셋을 다시 push할 때 .uasset이 Perforce read-only면 임포트가
  조용히 실패한다 → 먼저 p4 edit로 체크아웃할 것
- 로그/리포트: `sk_batch/logs/` (저장소 루트 기준)

## 실패 복구 규칙

- Base/BaseRef 레코드는 SPM 전체 개수가 아니라 FBX에 실제 생긴 orphan `_Start` 본을
  기준으로 역방향 매칭한다. 본이 꺼진 generator의 Base 레코드는 제외한다.
- Base 연결과 독립 root branch가 섞인 파일은 Base로 확인된 root만 원래 가지에 붙이고,
  남는 top-level root만 true root에 연결한다.
- 요청한 `Bone_1_Start`가 없으면 실제 root 중 가장 앞선 본(예: `Bone_1_End`)을 true root로
  추론한다.
- SpeedTree가 Relative 본 설정 또는 외부 atlas cutout 참조 때문에 armature-only FBX를
  만들면, 임시 FBX의 `Vertices` 존재 여부로 감지한다. root-only generator는
  `Absolute/1`로 되돌리고, 외부 Frond cutout 참조만 가장 가까운 내장/기본 material로
  복구한다. 추가된 atlas material asset 자체는 삭제하지 않는다.
- `BranchMesh`처럼 SPM에 Branch 본 generator 자체가 없는 자산은 FBX를 no-bones
  preset으로 내보내고, Blender에서 전체 geometry를 `Bone_1_Start` 하나에 rigid
  skin한다. XML에 본이 없어도 group 0 dynamic-wind JSON을 생성한다.
- 반면 보이는 Branch generator가 존재하지만 모두 `Absolute/0`이면 아직 SK용으로
  제작되지 않은 데이터로 판정한다. SPM을 수정하거나 장시간 export하지 않고 즉시
  실패 처리하며, JSON/GUI 오류에 generator 이름과 style/bones 값을 기록한다.
- 같은 SPM의 FBX/XML은 SpeedTree 프로세스 두 개로 동시에 열지 않고 순서대로 export한다.
  서로 다른 SPM의 ① 캘리브레이션 병렬 처리는 그대로 유지한다.
- 실패 중 생성된 최신 `.blend`만 보고 완료로 건너뛰지 않는다. blend와 wind JSON이 모두
  존재하고 SPM보다 최신일 때만 ②를 건너뛴다.
