# PCG ST9 → SK 전환 준비 보드

언리얼 PCG와 작업 레벨에 직접 배치된 ST9 나무(WPO + 마스크 머티리얼)를
**SK_ 데이터(나나이트 + 논마스크 지오메트리 + 버추얼 텍스처)** 로 바꾸기 위한 상태 보드.

실행:

```bat
PCG_ST9_Texture_Batch.bat
```

①/②/③과 Atlas 대상 해제는 SK Batch·SPM Generator Sync와 같은 프로세스 간
공용 FIFO에 들어간다. 다른 BAT 창에서 실행해도 실제 변경 작업은 겹치지 않는다.
일반 ③과 `③ 전체 다시 뽑기`의 무거운 대상 계획은 공용 실행 차례가 온 뒤
백그라운드에서 최신 상태로 다시 계산하므로, 대기 중 GUI가 멈추거나 오래된
렌더 계획을 그대로 재생하지 않는다.

## 시작 시 표 갱신

보드는 마지막으로 성공한 live 감사 결과를
`%LOCALAPPDATA%\SpeedTreeBatchTools\cache\board_snapshot_v2.json`에 표시 전용으로 저장한다. 다음 실행에서는
이전 표를 먼저 보여 주고 `live 검증 중` 상태에서 모든 변경 버튼을 잠근다. 이
스냅샷은 완료 영수증이나 실행 허가로 사용하지 않는다. 설정, Tree root 또는 PCG
대상이 달라도 이전 표라는 사실을 표시할 뿐 작업 성공으로 판정하지 않는다.

SPM/SBS/Blend 분석 캐시도 같은 사용자별 디렉터리에 저장하므로 checkout과
worktree가 같은 파일을 재사용한다. 테스트나 격리 실행은
`SPEEDTREE_BATCH_TOOLS_CACHE_DIR` 환경 변수에 절대 디렉터리 경로를 지정해 이
위치를 바꿀 수 있다. Windows 외 환경에서는 `$XDG_CACHE_HOME/SpeedTreeBatchTools/cache`
(미설정 시 `~/.cache/SpeedTreeBatchTools/cache`)를 사용한다.

`board_snapshot_v2.json`은 UTF-8 직렬화 기준 최대 16 MiB이며 최신 파일 1개만
유지한다. 새 스냅샷이 한도를 넘으면 디스크에 쓰지 않고 기존의 마지막 정상
스냅샷을 유지하며, 한도를 넘는 기존 파일은 읽기 단계에서 표시 캐시로 거부한다.
v2 projection은 표 렌더링에 쓰지 않는 lineage, assembly handoff, 상세 Generator
binding 진단만 생략하고 폴더/상태/action 및 연결 완료 표시는 보존한다. 생략 필드와
개수, 직렬화 byte 수는 snapshot/latency receipt에 기록된다.

SPM semantic cache는 파일 시각만 신뢰하지 않고 안정적으로 읽은 전체 SPM bytes의
SHA-256에 묶인다. 그 검증 read의 bytes를 즉시 decode/parser에 넘겨 같은 SPM을 다시
열지 않는다. 캐시는 계산 memoization일 뿐 실행 권한이 아니며, 변경 작업 worker는
시작 직전에 선택 행의 현재 live evidence를 다시 검증한다. primary가 완료되면 이
memoization과 display projection을 relation 계산 전에 각각 원자적으로 저장한다.
relation 중 입력 변경은 계속 fail-closed지만 이미 끝난 primary 계산은 다음 실행의
warm cache로 남는다. 취소·교체된 refresh generation은 cache 파일을 publish하지 않는다.
relation/live-mutation token은 sampled key가 아니라 전체 파일 SHA-256과 디렉터리
membership으로 계산하며, 공유 입력은 generation-local memo로 한 번씩만 읽는다. 실행
직전에는 선택 행의 full-content token을 다시 계산하므로 대형 파일의 sample window 밖
same-size/restored-mtime 변경도 실행 권한을 상속하지 못한다.

새 live 기본 감사가 끝나면 ①–③ 상태 열을 먼저 교체한다. 비용이 큰
`Blend ↔ SPM` 관계 열 계산과 분석 캐시 저장은 그 뒤 백그라운드에서 완료하며,
full-content relation 검증 뒤에만 해당 변경 버튼을 다시 연다. sync migration은
relation과 동시에 시작하며 먼저 끝난 sync 결과는 relation Treeview 갱신에 합쳐 전체
delete/reinsert를 반복하지 않는다. 따라서 전체 관계 계산 때문에 첫 표가 늦게 나타나는
구조로 되돌아가지 않는다.

## 화면 구성

- **표**: 나무 폴더는 공유 작업 그룹이고, 바로 아래에 실제 또는 생성 예정
  `SK_*.spm`이 각각 체크 가능한 행으로 모두 보인다. 컬럼 순서 = 작업 순서.
- **행 클릭** → 아래 상세 패널에 "이 폴더는 뭐가 되어 있고, 다음에 어느 프로그램에서
  뭘 하면 되는지"가 문장으로 나온다. 파일 저장이나 다른 창 열기 없음.
- **로그**: 실행/건너뜀 내역과 이유.
- 폴더 체크는 자식 SPM 전체를, SPM 체크는 해당 모델 하나만 선택한다. ①은 체크된
  SPM만 정확히 처리하고, ②·③은 선택된 SPM이 속한 폴더의 공유 작업을 한 번만 처리한다.

## 단계 (표의 컬럼)

| 컬럼 | 뜻 | 누가 하나 |
|---|---|---|
| ① SK + 머티리얼 이름 | 일반 식생과 Cluster output을 canonical `SK_*.spm`으로 정규화한 뒤 머티리얼 `M_` 이름 및 공용 이름을 정리 | **[① 실행] 버튼이 자동 처리** |
| ② 잎 메시 (Blender) | 아틀라스 리프 제너레이터로 오파시티 없는 잎 지오메트리 생성 | **[② 실행] 버튼이 자동 처리** (헤드리스 Blender) — 직접 할 값도 상세 패널에 나온다 |
| ③ 텍스처 (Substance) | 표시된 SpeedTree Generator가 사용하는 고유 연결 텍스처 세트마다 `T_` 6장 추출 | **[③ 실행] 버튼이 자동 처리** (sbsrender) — SBS 그래프와 SPM 연결도 `T_`로 관리 |
| ④ Blend ↔ SPM 확인 | 실제 `.blend` 파일별 대상 목록과 현재 SPM Generator 연결 상태 감사 | **JSON 공동 관리** — 기본 SPM 행은 폴더 바로 아래에 항상 보이고, blend 행을 펼치면 해당 연결 문맥의 SPM과 상태가 나온다. 선택 blend에 SPM을 추가하거나 선택 SPM을 목록에서 제거할 수 있다 |

④의 대상 목록은 각 blend 옆 `<blend stem>.atlas_leaf_targets.json`을 기준으로 집계한다. 이 파일은 Blender `atlas_leaf_mesh_builder` 애드온과 공유된다. `선택 SPM 제거` 또는 Atlas 자식 행의 `Delete`는 실제 `.spm` 파일을 삭제하지 않고 먼저 `_spm_backups` 백업을 만든 뒤 원래 Generator 슬롯을 복원하고 해당 blend scope의 Material_v8/Mesh 자산만 SPM 내부에서 제거한다. 구형 manifest에 원래 슬롯 값이 없으면 해당 owned 슬롯을 `Material=-1`, `Mesh=-10`으로 해제한다. 검증이 끝난 뒤에만 JSON에서 빠진다. JSON이 아직 없는 기존 blend만 감사 결과를 임시 fallback으로 사용한다. 목록에 있으나 현재 경로에 없는 SPM은 `파일 없음`으로 유지해 잘못된 경로나 이동된 파일을 확인할 수 있게 한다.

SK SPM 리페어 `.blend`의 상태 확인과 교체는 `..\sk_batch\SK_Batch.bat`가 단독으로 담당한다.
이 보드는 해당 상태를 판정하거나 작업 완료 여부에 포함하지 않는다.

## [① 실행] — SK 만들기 + M_ 이름 붙이기

- 체크된 행 중 ①이 필요한 항목에만 적용된다.
- 수정 전 원본은 각 폴더의 `_spm_backups\` 에 백업이 남는다.
- 실행 전 확인창에 "SK 몇 개 생성, 이름 몇 개 변경"이 정확히 표시된다.
- Cluster의 최초 무접두사 상태는 해당 SPM을 canonical `SK_*.spm` output으로 한 번
  정규화한다. 이후 `M_` 정리, SpeedTree export, Blender와 Unreal 입력은 canonical SK만
  기준으로 하며 canonical 내용을 예전 무접두사 파일로 다시 게시하지 않는다.
- Cluster 안의 모든 named Material_v8는 일반 `M_` 규칙으로 정리한다. 실제 export 참여
  재료가 canonical `M_*` 이름을 유지하며, 같은 이름으로 겹치는 비출력 legacy 재료는
  `_old`, `_old_02` 순으로 결정적으로 보존한다. 같은 canonical 이름을 요구하는 재료가
  둘 다 export 참여 상태이면 자동 추측하지 않고 차단한다.
- canonical 생성 뒤 남은 무접두사 파일은 레거시 입력 증거로만 보존한다. 그 파일이
  바뀌어도 canonical output으로 역복사하거나 현재 output 판정을 바꾸지 않는다.
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
Cluster SPM은 ② 원본 아틀라스 추적 근거일 뿐이며 ②의 대상 개수와 파일 수정에서는 제외한다.
Cluster SPM pair의 최초 canonical `SK_` 생성과 머티리얼 정리는 ①에서만 수행한다.
무접두사 SPM은 TGA·카메라 원본으로 남으며 canonical 내용을 그 파일에 다시
게시하지 않는다.
최종 SPM의 옛 Cluster Generator도 같은 원칙이다. 공용 Legacy Cluster 계약의
receipt에 기록된 Generator GUID는 숨김/표시 여부와 무관하게 과거 출처로만 보존한다.
receipt가 없는 현재 Generator만 실제 export 참여 여부를 보고 ② 작업 대상으로 센다.
또한 현재 Material+Mesh 연결과 같은 이름의 Blender 아틀라스가 이미 완성돼 있으면
예전 원본 텍스처가 남아 있어도 재제작 작업으로 되돌리지 않는다.

- 다른 폴더의 아틀라스를 쓰는 경우(densiflora→scotspine)는 **그래프가 있는
  폴더 소유**로 정리된다: 작업은 소유 폴더 행에 나오고, 사용하는 폴더에는
  "공유 — 그쪽 행에서 처리"로 표시된다.
- 출력 텍스처는 sbs 옆, `texture\`, `texture\substance\` 를 모두 뒤져서 찾는다.

## Cluster → Assembly dependency와 handoff

기존 감사와 같은 행 안에서 Tree/Bush/Weed의 실제 `Cluster` 폴더와 그 직하의
SPM pair를 파일별 독립 행으로 표시한다. `branch_*`/`leaf_*`뿐 아니라
`cluster_*` 같은 기존 이름도 숨기지 않는다. 각 행에는 canonical `SK_*.spm` output,
레거시 무접두사 입력과 이름 정규화 상태가 함께 표시된다. 별도 Cluster 버튼은 없으며
일반 ① 선택에 포함된다. 무접두사 파일만 있으면 canonical `SK_` output으로 한 번
정규화한다. 이후 SK Batch, SpeedTree 렌더·FBX·STMAT·보고서와 Unreal Push는 모두
canonical `SK_` stem을 사용한다.

SpeedTree Cluster Normalizer가 만든 `Cluster/SK_<원본>.blend`도 같은 Cluster 하위에
표시한다. 해당 blend는 부모 식생 폴더 전체에 대해 관계 하나만 가지며, Base나 SK별
ON/OFF 행 없이 `ON`/`OFF`로 표시한다. `ON`은 폴더 직하 모든 `SK_*.spm`, `OFF`는
전체 해제다. 일부만 연결된 기존 상태는 `PARTIAL`로 감사한다. blend 옆
`.atlas_leaf_targets.json`을 SPM Generator Sync와 공유하며 PCG 보드는 이 관계와
실제 Generator 연결 완료 여부를 읽기 전용으로
보여 준다. 관계 변경, 기존 `M_*` embedded mesh 제거, 정규화 mesh 삽입 및 OFF 복원은
SPM Generator Sync가 Atlas Leaf Mesh Builder를 호출하여 수행한다.

legacy 항목의 `Cluster 출력 TGA 연결 N장`은 해당 Cluster SPM이 최종 렌더 결과로
연결한 TGA 경로 수다. `PHYSICAL_DIRECT_CAPTURE` 항목은 Color/Opacity를 포함한
같은 Blender 촬영 영수증과 해상도를 확인해 `Blender 촬영 1024² · 완료`처럼
표시하며, 호환용 기본 맵까지 합친 8장 수를 완료 의미로 노출하지 않는다.
실제 파일이 없으면 `누락 M장`을 따로 표시한다. 잎 메시 ②가
추적하는 내부 원본 알베도/알파나 머티리얼 슬롯 수와는 다른 값이다. 같은 자산
아래에는 추상적인 `Blender` 보조 행 대신 감사에서 확인된 실제 `.blend` 파일명이
별도 관리 행으로 표시된다. 파일 행은 기본적으로 접혀 있으며 펼치면 해당 blend가
적용되는 최종 SPM과 Generator 연결 완료/점검/미확인 상태를 확인할 수 있다. blend
행은 실제 `.blend` 경로를, 자식 SPM 행은 해당 SPM 경로를 복사한다. 이 관계 표시는
①~③ 작업 단계와 별개의 관리 정보이며 파일이나 폴더를 새로 만들지 않는다.

Assembly 역할은 이 전체 inventory와 분리한다. export FBX에서 material–mesh
완전쌍이 확인된 `branch`/`leaf`만 Assembly 후보가 되고, 일반 `cluster_*` 행은
독립 SPM 처리 대상으로 그대로 남는다.

branch/leaf 처리는 export FBX 내용으로 각각 독립 판정한다.

- 해당 역할 material이 mesh Model에 연결된 완전쌍이면 Assembly part 정규화 후보
- material과 역할 mesh가 모두 없으면 기존 Full SK 흐름을 그대로 통과
- 둘 중 하나만 있거나 FBX를 읽을 수 없으면 해당 역할을 차단하고 오류 근거 기록

감사 JSON의 각 항목에는 `cluster_assembly` 전체 근거와
`assembly_handoff` receipt가 함께 들어간다. receipt에는 Tree/Cluster SPM hash,
source material·mesh·texture dependency, FBX material–mesh 연결, canonical bark 교체
판정, TGA basename 검증, reference mesh 근거와 차단 사유가 포함된다. 후속 SK
Batch/Assembly 단계는 receipt를 입력 근거로 쓰되 실제 FBX를 다시 검증해야 한다.
Full SK와 별도 Nanite Assembly는 같은 최종 생성 Skeleton을 사용하고, wind JSON과
DynamicWind 데이터는 그 Skeleton 기준으로 재생성·bone index·binding hierarchy를
검증한다. 기존 production DynamicWind 데이터를 복사하는 계약은 사용하지 않는다.

GUI의 초기 검사·새로고침과 ①–③ 완료 후 재검사는 이 계약을
`reports\cluster_assembly\cluster_assembly_{폴더}_{source-path-hash}.json`에
atomic write한다. 동일 내용은 다시 쓰지 않으며 원본 SPM/TGA에는 쓰지 않는다.
감사 상태도 실제 디스크 동작을 그대로 구분한다. 새 bytes를 쓴 경우만
`RECEIPT_PERSISTED/written`이고, 동일 내용으로 mtime을 보존한 경우는
`RECEIPT_UNCHANGED/unchanged`이다.
후속 파이프라인은 `locate_cluster_assembly_receipt(spm_path)`로 SK SPM 또는 일반
source SPM에 해당하는 receipt를 정확히 하나만 선택할 수 있다. 선택 시 Tree/Cluster
SPM과 Cluster TGA SHA-256을 다시 확인하므로 변경된 stale receipt는 거부된다.

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
  일반 버튼 클릭 자체를 실행 승인으로 보므로 긴 대상 계산 뒤에 다시 확인을 묻지 않는다.
  계획·이름 오류 항목은 자동 제외하고 정상 항목을 끝까지 처리한 뒤, 완료 상태와 실행별
  `reports/step3_run_*.json`에 제외 대상과 사유를 기록한다.
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

### canonical output manifest

PCG ST9 Texture/SBS가 새 6장 세트를 성공적으로 검증하면 asset의
`texture/pcg_st9_canonical_outputs.json`을 원자적으로 갱신한다. 이 파일은
`kind=pcg_st9_canonical_output_manifest`, `schema_version=1`이며 각 output에
`texture_base`, 6개 `required_roles`, manifest-relative `files`,
`material_targets`(`material_id` 우선, `material_name` 보조),
`producer.tool/source`를 기록한다. 필수 역할은
`color/opacity/normal/height/extra/subsurface`다. Height에서 만든 AO만
`texture/_pcgtex_generated/T_*_ao_from_height.*`로 추가할 수 있다.

`D:\OneDrive\Forestportfolio\Texture` 및 TCom/Megascans 파일은 SBS authoring
입력이다. manifest의 `files`나 production SpeedTree material `TexFilename`으로
사용하지 않는다. Atlas Blender mesh-build의 원본 입력은 별도 제작 단계라 이
규칙으로 치환하지 않는다. Blender Cluster bake/export 결과도 별도
`origin_kind=blender_cluster_bake` 계약이며, PCG material output manifest가
비-`T_` bake 이름을 `T_*` 누락으로 추측하지 않는다.

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

### Stale saved Node-table recovery

`NORMALIZED_GENERATOR_NODE_TABLE_STALE` uses an interactive recovery boundary:

```bat
python -m pcg_st9_texture_batch.stale_node_table_recovery ^
  "<SPM>" ^
  --expected-mesh-id 130 --expected-mesh-id 131 ^
  --expected-mesh-id 132 --expected-mesh-id 133
```

`--expected-mesh-id` is the backward-compatible strict mode: every listed ID
is sealed as both an authoring-binding target and a required live/export target.
When issue acceptance requires authoring continuity without universal export,
use the explicit sealed-scope mode instead:

```bat
python -m pcg_st9_texture_batch.stale_node_table_recovery ^
  "<SPM>" ^
  --authoring-mesh-id 14 --authoring-mesh-id 15 ^
  --authoring-mesh-id 16 --authoring-mesh-id 17 ^
  --required-live-mesh-id 14

python -m pcg_st9_texture_batch.stale_node_table_recovery ^
  "<SPM>" ^
  --authoring-mesh-id 14 --authoring-mesh-id 15 ^
  --authoring-mesh-id 16 --authoring-mesh-id 17 ^
  --no-required-live-delivery
```

Explicit mode requires a non-empty authoring scope and either a repeated live
subset or the explicit no-live-delivery flag. Mixing legacy and explicit modes,
omitting the live decision, selecting a live ID outside the authoring scope, or
changing the caller scope after sealing fails closed before Modeler launch.
Receipts from schemas 2 through 4 remain strict-all and are never rewritten.

The immutable receipt compatibility matrix is literal and independent of the
current projection constants:

| Receipt schema | Graph | Core | Membership | Targets | Requirements | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 2 | 1 | — | 1 | 1 | — | supported |
| 3 | 1 | 1 | 1 | 1 | — | known, unsupported |
| 3 | 1 | 2 | 1 | 1 | — | supported |
| 4 | 1 | 3 | 1 | 2 | — | supported |
| 5 | 1 | 3 | 1 | 2 | 1 | supported |
| 6 | 1 | 4 | 1 | 2 | 1 | supported (current) |

Each supported tuple resolves through an immutable semantic registry entry
that owns its frozen graph/core/membership/target projector callables and the
authoritative fields that must match as one candidate. In particular, a
target-v1 fingerprint cannot borrow a binding count or Mesh-ID list from a
different historical candidate. The current writer also selects the explicit
current dialect tuple (schema 6 here) instead of assembling versions from
mutable current constants.
Independent literal `backup.spm`, `receipt.json`, `after.spm`, and
`expected.json` fixtures under `tests/fixtures/issue_41/` exercise both
read-only `verify_sealed_resave()` and interrupted pre-save restart paths;
their receipt bytes are not produced by the production helpers.

Every supported historical fingerprint and its authoritative counts/Mesh-ID
lists are recomputed from the exact backup before the current projection is
derived. A valid sealed receipt is reused byte-for-byte; it is never upgraded
or rewritten in place. Unknown receipt schemas fail with
`preimage_receipt_schema_unsupported`; known or unknown unreproducible inner
projection tuples fail with `preimage_receipt_projection_version_unsupported`;
malformed content, fake fingerprints, and source/scope mismatches fail with
`preimage_receipt_verification_failed`. Backup-byte mismatch remains
`preimage_backup_verification_failed`.

Backup authority always comes from a fresh immutable capture of the backup
path itself. Operating-source snapshot bytes are never substituted for backup
bytes, and the backup is recaptured immediately before Modeler launch and
again immediately before any continuation claim. A backup race at either
boundary fails closed without launch, callback, or claim creation.

Before Modeler is opened, the command captures an exact byte-for-byte preimage
under `_spm_backups/stale_node_table_recovery/` and verifies an immutable
SHA-bound receipt. The receipt contains versioned authoring-graph, Generator
membership, required target-binding fingerprints, and immutable schema-5
authoring/live scope requirements. New receipts use schema 6 with the same
sealed-scope contract. It then waits for the
user to save the file and requires repeated identical stat/size/SHA snapshots
with successful parsing. Regex, independent ElementTree, target delivery, and
normalization evidence all come from those same immutable bytes. All authoring
bindings, including hidden bindings, remain fingerprinted; live Node/export and
normalization requirements apply only to the sealed required-live subset. A
continuity-only receipt records normalization as not applicable, not complete.

The command does not edit SPM XML, automate Save or keystrokes, kill Modeler,
roll back automatically, or continue merely because `stale=false`. Library
callers may resume the initiating job only once, bound to its generation and
the verified after SHA, after cancellation/app-close/stale-job guards and a
final source-SHA recheck. A privacy-safe blocked-event receipt records only the
asset name, after SHA, and stable reason tokens. Missing/corrupt preimage or
receipt evidence fails before Modeler launch.

Core projection v4 hashes the complete ordered XML tree and removes or
canonicalizes only path-specific no-edit Save rewrites reproduced across three
exact before/after SPM pairs. It excludes the root session/generated blocks
`Thumbnail`, `ThumbnailSize`, `Preview`, `Statistics`, `TreeInfo`,
`QuickSaveSettings2`, `m_sTimelineData`, `Window`, and `Nodes`; generated GUIDs
only at the proven Light/Fan/RuleScript/Force/Link/Assets paths; false-only
generated collection rows at Generator and Force property paths; exact default
AtlasMaker, material-map, atlas-mesh UserData, empty LOD, and redundant parent
spline shapes; and Material preview/stream caches. It canonicalizes Generator
and Link endpoint GUID spellings, the observed spline/mesh/color float rewrites,
derived material texture sizes, and the stable direct-Assets kind partition
while preserving order within every partition. Namespace-qualified or unknown
elements, authored properties, arbitrary UserData, non-default shapes,
non-false collection rows, full Link subtrees, material filenames, mesh data,
and all other root/settings content remain fingerprinted. Historical schemas 4
and 5 continue to verify with the frozen core-v3 projector, while a successful
reaudit derives current core-v4 evidence from the exact backup without rewriting
the sealed receipt.

개별 산출물: `export_prepare_plan.py`(SK/M_ 변경 예정 목록), `export_prepare_apply_queue.py`
(`--apply`로 안전 항목 일괄 적용), `export_texture_plan.py`(②③ 작업표),
`export_atlas_handoff_queue.py`(Blender 핸드오프),
`export_sbs_handoff_queue.py`(Substance 핸드오프), `export_review_queue.py`/`export_review_brief.py`
(수동 확인 목록). 모두 읽기 전용이며 GUI와 같은 검사 엔진(`pcg_texture_audit.py`)을 쓴다.
