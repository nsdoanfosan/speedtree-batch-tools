# SPM Generator Sync

같은 수종의 SpeedTree SPM을 `마스터 → 자식` 제작 계보로 관리하는 세 번째 독립 도구입니다.

```text
weed_black_locast
├─ ◆ SK_tree_black_locast_01.spm  [MASTER]
│  ├─ ↳ SK_tree_black_locast_02.spm [FOLLOWER]
│  └─ ↳ SK_tree_black_locast_03.spm [FOLLOWER]
└─ ○ SK_tree_black_locast_04.spm  [INDEPENDENT]
```

`SPM_Generator_Sync.bat`을 더블클릭해 실행합니다.

적용·동기화·Cluster 관계 변경/갱신은 PCG와 SK Batch가 공유하는 프로세스 간
FIFO에 클릭 즉시 등록된다. 같은 창의 로컬 대기열뿐 아니라 다른 BAT 창의 작업도
같은 순서를 사용하며 실제 변경은 한 번에 하나만 실행한다. Cluster 갱신은 자기
차례가 왔을 때 registry와 manifest를 다시 읽고, 그 시점에도 완전한 ON인 대상만
처리한다.

## 무엇을 동기화하는가

- 마스터 Base 아래 공통 Generator 속성과 곡선
- Base 이름·GUID·Base filter는 읽기만 하며 절대 변경하지 않음
- BaseRef 표시 이름만 Base 기반의 고유한 export-safe 형식으로 정리
  - 예: `Ref_Leaf_3_001`, `Ref_BranchBig_2_001`
  - 영문·숫자·밑줄만 사용하고 전체 Generator 이름과 충돌하면 다음 번호 사용
  - BaseRef의 계층 순서에 따라 같은 Base 안에서 번호 부여
- 마스터와 현재 SPM 사이에 차이가 있는 Generator 구조
- 마스터에만 있는 Generator 구조를 선택 자식에 추가
- 관리 Base에서 자식에만 있는 초과 Generator 구조는 삭제하고 부모·형제에는 절대 전파하지 않음
- 자식에 대응 Base 자체가 없으면 해당 Base와 하위 구조를 자식 `Tree` 아래에 새로 연결
- 새 Generator의 Material/Mesh 참조는 마스터의 숫자 ID를 그대로 복사하지 않고, 에셋 이름을
  기준으로 자식 SPM의 로컬 ID로 변환
- 필요한 머티리얼이 자식 SPM에 없으면 머티리얼 정의와 연결된 Cutout/Supplemental Mesh를
  충돌 없는 새 로컬 ID로 함께 복사한 뒤 새 Generator에 연결
- 과거 동기화에서 Leaf 속성이 cluster 재질처럼 다른 역할의 에셋을 가리키게 된 참조도 자동 교정
- Leaf / Branch / End 역할별 아이콘 배경색
  - Leaf: 녹색
  - Branch: 파란색
  - End: 빨간색
- 같은 Base를 사용하는 BaseRef에도 같은 색 적용
- `BranchBig`/`BranchSmall`처럼 같은 계열의 서로 다른 Base는 같은 색조 안에서 밝기를 다르게 표시

기본적으로 다음 항목은 자식의 값을 보존합니다.

- Generator 이름과 GUID
- Random Seed
- 재질, 메시, Collection/asset 참조
- BaseRef의 배치/Generation 설정
- 기존 자식 Generator의 `Generation > Pass`
- Node/Freehand Edit (`Nodes` XML은 수정하지 않음)

`Pass`는 자식별 Reference/Base 계산 순서에 속하므로 마스터 값으로 덮어쓰지 않습니다. 동기화 후에는
SpeedTree 규칙에 따라 일반 계층의 `Parent pass <= Child pass`와
`Reference pass < 참조 Base pass`를 정적으로 검사합니다. 기존 값을 내리지 않고 필요한 값만 올리며,
Base 아래의 재사용 템플릿 subtree에는 Base pass를 전파하지 않습니다. Base filter가 비어 있으면 모든
Base를 대상으로 보고, `|`, `&`, `!`, `()`, `*`, `?`, 따옴표, `=`, `==` 검색 문법도 해석합니다.

기존 자식 Generator가 정상적인 같은 역할의 다른 재질을 사용하는 경우에는 해당 변형을 보존합니다.
로컬 ID가 없거나 `Leaf → cluster`처럼 역할이 충돌하는 경우에만 마스터의 에셋 이름을 자식 에셋
테이블에서 다시 찾아 안전하게 교정합니다. 이름이 같은 에셋도 없으면 마스터의 에셋 정의를 복사하며,
미리보기 상세에는 `에셋 복사` 항목으로 머티리얼과 메시 이름 및 새 ID를 표시합니다.

마스터와 현재 SPM의 같은 부모 아래에서 Generator Type별 순서를 기준으로 공통 노드를 대응시킵니다.
구조 동기화 방향은 항상 `마스터 → 자식`입니다. 마스터에만 있는 구조는 자식에 추가하고, 관리
Base에서 자식에만 있는 구조는 적용 시 삭제합니다. 자식 구조·속성·에셋이 마스터나 다른 자식으로
승격되는 경로는 없습니다. BaseRef 배치와 `독립 Base`는 파일별 데이터로 남겨 구조 동기화에서
제외합니다. 행을 선택하면 상세에서 추가될 마스터 구조와 삭제될 자식 초과 구조를 구분해 확인할 수
있습니다.

## 사용 순서

1. `나무 루트`를 선택하고 `폴더 다시 검사`를 누릅니다.
   - 기본 `SK_ SPM만 보기`를 끄면 폴더 안의 모든 SPM을 표시합니다.
   - SPM 행을 선택하고 `Ctrl+C` 또는 `선택 경로 복사`를 누르면 `D:\...\파일.spm`
     전체 경로가 클립보드에 복사되어 Everything 검색창에 바로 붙여넣을 수 있습니다.
   - 여러 행은 한 줄에 하나씩 복사되며, 폴더 행은 폴더 전체 경로를 복사합니다.
2. 자식으로 만들 SPM 행을 마스터 SPM 행 위로 드래그해 놓습니다.
   - 마스터 후보·미지정·독립 행 위에 놓으면 그 행이 마스터로 확정됩니다.
   - 이미 연결된 자식을 다른 마스터 위에 놓으면 새 마스터로 재연결됩니다.
   - 여러 행을 선택한 뒤 함께 드래그할 수도 있습니다.
   - 기존 `선택을 마스터로`, `선택을 자식으로 연결` 버튼도 보조 방식으로 유지됩니다.
   - 마스터로 확정되는 즉시 자식 유무와 관계없이 Base 분류, BaseRef 이름, 아이콘 색상이 SPM에 적용됩니다.
     승격 전 원본과 기존 관계 설정은 `_spm_backups/master_promotion_*`에 백업됩니다.
3. 자식 Base별로 따라갈 마스터 Base를 확인합니다.
   - Base 이름은 변경되지 않으므로 여러 자식 Base가 같은 마스터 Base 제작 규칙을 따라갈 수 있습니다.
   - 구조가 다른 두 Branch를 별도로 관리하려면 마스터도 `BranchBig`, `BranchSmall`처럼 Base를 분리하거나 하나를 `독립 Base`로 둡니다.
   - 동기화하지 않을 Base는 `독립 Base`로 둡니다.
4. 마스터의 `Base 색 분류`를 확인합니다.
5. `변경 미리보기`로 마스터와 동기화될 구조·속성·색상 변경을 확인합니다.
6. 선택 자식 또는 마스터의 모든 자식을 동기화합니다.
   - 하단 진행 표시줄에서 현재 파일과 `패치 계산 → XML 검사 → SpeedTree 사전검사 → 백업 → 저장`
     단계를 확인할 수 있습니다.
   - 실제 SpeedTree 계산 중에도 경과 시간이 계속 갱신되므로 작업이 멈춘 것인지 기다리는 중인지
     구분할 수 있습니다.
7. 보드 전체의 확정된 연결을 한 번에 처리하려면
   `연결 전체 동기화 + Cluster 갱신`을 사용합니다.
   - Base 매핑이 확인된 `마스터 → 자식` 그룹을 먼저 동기화한 뒤, 관계가 정확히 `ON`인
     Cluster만 현재 연결 대상에 재적용합니다.
   - 독립·미지정 SPM과 `OFF`·`PARTIAL` Cluster는 변경하지 않습니다. Base 매핑 미확정이나
     크기 폭증 위험 자식도 제외 사유로 남깁니다.
   - 한 그룹 또는 Cluster가 실패해도 나머지는 계속 처리하며, 실행별 결과는
     `spm_generator_sync/reports/connected_sync_cluster_refresh_*.json`에 기록합니다.
   - 다른 작업이 진행 중이면 기존 FIFO 대기열에 들어갑니다. 실제 차례가 시작될 때 계보
     manifest와 Cluster registry를 다시 읽으므로, 앞 작업에서 OFF가 된 관계를 오래된
     화면 스냅샷으로 다시 ON 처리하지 않습니다.
   - v2 보고서는 mutation 전에 생성되고 각 시도 시작/실패 및 단위 완료 때마다 fsync 뒤
     원자적으로 checkpoint됩니다. `run_id`, queue 소유권, 순서가 고정된 `unit_id`,
     성공/실패/실행 중 상태, SHA-256 dependency identity, 실패 분류와 bounded retry 시도를
     보존합니다. checkpoint가 실패하면 다음 mutation은 시작하지 않습니다.
   - 일부만 실패하면 UI와 공용 FIFO receipt 모두 `partial`을 `failed`와 구분해 표시합니다.
     receipt에는 Generator/Cluster 성공·실패 수와 정확한 보고서 path/SHA-256/size가 남습니다.
   - `연결 실패 단위만 재시도`는 임의의 reports 폴더 파일이 아니라 terminal 공용 FIFO
     receipt가 정확한 path/SHA-256/size로 봉인한 v2 partial 보고서만 읽고 현재 보드를 다시
     스캔합니다. 전체 단위 순서, 설정/도구/코드, SPM/blend/registry, Atlas
     target/scope/global receipt, normalization receipt, isolated-source cache 및 생성물 디렉터리
     inventory와 Blender가 실제 로드한 `atlas_leaf_mesh_builder`/
     `speedtree_cluster_normalizer` add-on code manifest가 모두 같을 때만 실패 단위를 선택합니다.
     inventory는 파일 hash 전후 두 번 열거해 중간 add/remove 경쟁도 불안정으로 처리합니다.
     매 시도 뒤 기존 성공 단위를 다시
     검증합니다. 보수적 read/write set과 overlap graph에서 기존 성공 단위와 생성물을 공유하는
     실패 단위는 mutation 전에 failed-only retry 부적격으로 처리하고, 예상 밖 드리프트가
     생겨도 다음 단위를 실행하지 않은 채 새 전체 계획을 요구합니다.
   - JSON atomic publish가 구조화된 pre-commit/rollback 성공 증명을 제공한 permission/lock
     오류만 현재 공용 queue lease와 동일 dependency identity 아래에서 0.2초/0.5초 backoff로
     최대 3회 시도합니다. 각 attempt 시작 직전에 전체 identity를 다시 캡처하며 Cluster는 설치된
     add-on producer probe도 다시 실행하므로 plan 이후나 backoff 중 변경은 mutation 전에
     중단됩니다. 일반 JSON 읽기 오류, rollback 실패, content drift, persistent denial은 재시도해
     숨기지 않고 보고서에 남깁니다.

파일명의 `_01`은 화면에서 `MASTER 후보`로만 제안합니다. 이름만으로 관계를 확정하거나 파일을
수정하지 않습니다. 확정한 관계는 나무 폴더의 `spm_generator_sync.json`에 상대 파일명으로 저장됩니다.

## 안전 규칙

- 미리보기는 읽기 전용입니다.
- 적용 전에 모든 패치를 메모리에서 먼저 만들고 XML/Generator/Link/BaseRef 무결성을 검사합니다.
- SpeedTree 명령행 export가 성공해도 Generator 오류가 남을 수 있으므로 Pass 의존성은 별도의 정적
  검사로 저장 전에 차단합니다.
- 마스터와 자식의 `Tree > Shape:Radius`를 비교합니다.
  - 자식이 1.5배 이상 크면 주의로 표시합니다.
  - 2.5배 이상 크면 거리 기반 생성의 노드·폴리곤 폭증 위험으로 원본 쓰기 전에 차단합니다.
  - 큰 나무는 작은 나무 마스터에 연결하지 않고 큰 나무용 별도 마스터를 사용합니다.
- 마스터는 읽기 전용 기준이며 동시 변경 감지용 지문만 확인합니다.
- 선택 자식은 한 트랜잭션으로 처리하고, 마스터는 자식 Sync의 저장·백업·검증·롤백 대상에 넣지 않습니다.
- `SpeedTree 10.1 실제 검증`은 같은 폴더의 임시 복사본을 먼저 계산·XML export합니다.
  실패하거나 5분을 넘기면 임시 파일만 제거하고 원본은 수정하지 않습니다.
- 사전검사가 성공한 뒤 첫 저장 전에 `<나무 폴더>\_spm_backups\generator_sync_날짜시간\`에
  선택 자식을 백업합니다.
- 저장·무결성 검사·SpeedTree 검증 중 하나라도 실패하면 변경 대상 자식을 백업으로 복구합니다.

### 프로세스 출력과 취소 상태

- SpeedTree stdout/stderr는 프로세스가 끝날 때까지 모아 두지 않고 기존 GUI 작업 큐로
  줄 단위 스트리밍합니다. 줄바꿈 없이 끝난 마지막 부분 줄도 보존합니다.
- 두 파이프는 별도 reader가 동시에 비우며, Tk 위젯 갱신은 항상 메인 스레드의
  `_poll_job()`에서 제한된 묶음으로 처리합니다. producer→Tk 전달은 작업별 최근
  4,096줄과 단일 `output_ready` wake-up으로 제한하고 UI 로그는 최근 3,000줄을
  유지합니다. 누락이 생기면 `[process_output_omitted]` 증거를 마지막에도 남깁니다.
- 프로세스 진단 stdout/stderr는 채널별 256 KiB tail만 결과에 보존합니다. 줄바꿈 없는
  한 줄은 16 KiB fragment로 나누므로 reader pending과 최종 capture가 입력 크기에 따라
  무한히 증가하지 않습니다.
- `현재 작업 취소`는 해당 FIFO 작업의 취소 이벤트만 설정합니다. 실행 전 또는 단계 사이에는
  안전 경계에서 중단합니다. Windows에서는 root를 suspended로 시작해 private
  `KILL_ON_JOB_CLOSE` Job Object에 배정한 뒤 resume하므로 child/grandchild도 같은 소유
  단위에 들어갑니다. root 종료 요청 뒤 tree/pipe EOF가 제한 시간 안에 끝나지 않을 때만
  `TerminateJobObject` fallback을 사용하며 무관한 sibling process는 건드리지 않습니다.
- 결과는 `cancelled_before_launch`, `cancelled_at_safe_boundary`,
  `cancelled_after_exit`, `cancelled_terminated`, `cancelled_killed`로 구분합니다.
  취소는 실패로 합산하지 않으며, 연결 전체 실행 보고서와 공용 큐 결과에는
  `status: cancelled`와 `termination_state`를 기록합니다.
- 이미 관찰된 nonzero exit와 함수가 반환한 committed success는 늦은 cancel보다 우선합니다.
  tree 종료 실패는 취소로 바꾸지 않고 `process_*_failed`/`process_*_grace_expired`, rollback
  복원 실패는 `[transaction_rollback_failed]` reason token과 원인·복원 경로 evidence로
  실패 처리합니다.
- 창을 닫으면 대기 작업을 취소하고 활성 작업에 같은 cooperative cancel 이벤트를 보낸 뒤,
  Tk `after()`로 worker/lease 완료를 계속 확인합니다. 고정 5초 뒤 파괴하지 않으며 worker가
  실제 종료된 뒤에만 설정 저장과 standalone/통합 root 파괴를 수행합니다.

공식 근거:

- [Reference generator 설정](https://docs.unity3d.com/speedtree-modeler/manual/add-and-set-up-a-reference-generator.html)
- [Generation properties의 Pass 규칙](https://docs.unity3d.com/speedtree-modeler/manual/generation-properties.html)
- [Base filter 검색 문법](https://docs.unity3d.com/speedtree-modeler/manual/search-syntax.html)

## 성능

- 동기화 실행은 각 대상 SPM을 한 번만 압축 해제·XML 파싱하고, 같은 메모리 문서를
  마스터 구조 통합과 최종 반영 계획에 재사용합니다.
- 폴더 보드는 SPM 수정 시간과 크기를 키로 Generator/Link 분석·구조 비교·동기화 해시를 캐시합니다.
- 결과는 PC 로컬 `spm_generator_sync_cache.json`에 저장되어 파일이 바뀌지 않으면 반복 새로고침뿐
  아니라 프로그램을 닫았다 다시 열어도 기존 분석을 즉시 재사용합니다.
- 속성 해시는 XML 깊은 복사 없이 정규화된 구조를 사용합니다.

## 설정 파일

- 수종 관계: 각 나무 폴더의 `spm_generator_sync.json`
- PC별 경로와 마지막 루트: `spm_generator_sync_config.json` (Git 제외)

## Cluster Normalizer 관계 ON/OFF

식생 폴더 직하의 `Cluster` 폴더는 일반 MASTER/FOLLOWER 계보로 취급하지 않고,
부모 식생 행 아래에 별도 하위 항목으로 표시한다.

- `Cluster/SK_branch_elm_01.spm` 같은 `SK_` SPM이 canonical 3D 원본이다.
  무접두사 SPM은 legacy pair 증거일 뿐이며 production 촬영 입력으로 사용하지 않는다.
- Color/Opacity, capture frame, plan 외곽과 UV는 같은 Blender
  `PHYSICAL_DIRECT_CAPTURE`에서 만들며 SpeedTree camera UV는 참조하지 않는다.
- 같은 stem의 정규화 결과는 `Cluster/SK_branch_elm_01.blend` 하나다. 별도
  무접두사 blend를 만들지 않는다.
- 각 정규화 blend에는 Base 매핑이나 SK별 자식 행 없이 폴더 단위 관계 한 개만
  `ON`/`OFF`로 표시한다. 기존 데이터가 일부 SK에만 연결되어 있으면 `PARTIAL`로
  감사되며 다음 ON/OFF 적용으로 한 상태로 정규화한다.
- 관계 `ON`은 blend 옆 `<blend stem>.atlas_leaf_targets.json`에 기록된다.
  적용 시 부모 식생 폴더 직하의 모든 `SK_*.spm`을 한 번에 등록한다. 현재 SK Batch
  BWR 보고서와 canonical Cluster SPM 해시를 먼저 확인하고, Blender 5.1에서
  `PHYSICAL_DIRECT_CAPTURE` 8맵 촬영 → 연결 성분별 `part_root` Prototype →
  Plan Mesh → Atlas handoff를 자동 실행한다. Blender에서 Cluster Normalizer를
  따로 누를 필요가 없다.
- Cluster Sync는 대상 SPM의 Generator/Branch 구조를 자동 복원·교체·숨김하지
  않는다. 현재 존재하는 Frond/Leaf 슬롯만 Atlas 연결 근거로 사용하며 구조 교체는
  사용자가 별도로 확인한 경우에만 수행한다.
- Atlas Leaf Mesh Builder는 출력 이름과 같은 `M_*` Material_v8이 있으면 기존
  embedded mesh를 자동 생성한 plan mesh로 갱신한다. 출력 재질이 없으면 같은 역할
  이름의 새 Material/Mesh 자산만 생성하고 Generator 슬롯은 변경하지 않는다.
  기존 출력 재질이 실제 Frond/Leaf 슬롯에 연결된 경우에만 source Material ID를
  각 SPM에서 다시 해석하며, 다른 Atlas scope 소유 재료는 계속 차단한다.
- `Cluster/SK_*.spm`의 SHA-256 또는 Blender physical-capture contract가 마지막
  대상 scope manifest와 달라지면 `Cluster 원본 변경 · 갱신 필요`로 표시한다.
  원본 SPM이 바뀌면 SK Batch가 최신 BWR blend를 먼저 만들고, 이 도구의
  `Cluster 갱신`이 정규화/Plan/Atlas 단계를 다시 실행하여 관계가 ON인 부모
  `SK_*.spm` 전체에 적용한다. `PARTIAL`은 실행 가능한 관계가 아니라 기존 데이터의
  불일치 감사 상태이며, 갱신 전에 반드시 관계 ON 또는 OFF로 정규화한다.
- 관계 `OFF`는 그 blend가 관리한 Generator 슬롯·Material/Mesh만 manifest의 원본
  스냅샷으로 폴더의 모든 SK에서 복원하고 JSON 목록에서 모두 해제한다. 실제 SPM
  파일은 삭제하지 않는다.
- PCG ST9 Texture 보드는 같은 JSON을 읽어 관계를 표시하지만, 실제 ON/OFF 적용은
  이 도구가 단독으로 담당한다.

## SK Batch 연동 준비

엔진은 패키지 진입점을 제공하므로, 안정화 후 기존 SK Batch의 `0. Generator Sync` 단계에서
같은 트랜잭션 함수를 그대로 호출할 수 있습니다. 별도의 SPM 수정 로직을 복제하지 않습니다.

```python
from spm_generator_sync import apply_group_transaction

result = apply_group_transaction(
    tree_folder,
    master_name,
    selected_followers,
    speedtree_exe=speedtree_exe,
    xml_ini=xml_ini,
)
```

## CLI

```powershell
python .\spm_generator_sync\spm_generator_sync.py scan "D:\OneDrive\Forestportfolio\02_nature\Tree" --sk-only
python .\spm_generator_sync\spm_generator_sync.py inspect "...\tree_01.spm"
python .\spm_generator_sync\spm_generator_sync.py repair-passes "...\tree_02.spm" --speedtree-exe "...\SpeedTree_Modeler.exe" --xml-ini "...\Options_HI_Xml.ini"
python .\spm_generator_sync\spm_generator_sync.py preview-auto "...\weed_black_locast" SK_tree_black_locast_01.spm SK_tree_black_locast_02.spm SK_tree_black_locast_03.spm
python .\spm_generator_sync\spm_generator_sync.py preview "...\weed_black_locast" SK_tree_black_locast_01.spm SK_tree_black_locast_02.spm
python .\spm_generator_sync\spm_generator_sync.py verify-auto-copy "...\weed_black_locast" SK_tree_black_locast_01.spm SK_tree_black_locast_02.spm --speedtree-exe "...\SpeedTree_Modeler.exe" --xml-ini "...\Options_HI_Xml.ini"
```

## 테스트

```powershell
python -m unittest discover -s .\spm_generator_sync\tests -v
python -m py_compile .\spm_generator_sync\spm_generator_sync.py .\spm_generator_sync\spm_generator_sync_gui.pyw
```
