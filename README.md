# SpeedTree Batch Tools

SpeedTree 식생 변환 작업을 위한 독립형 Windows 배치 도구 모음입니다. Blender 애드온 본체와 분리해 GUI, 자동화 작업, 테스트를 이 저장소에서 관리합니다.

## 도구

- `SpeedTree_Batch_Tools.bat`: 아래 세 도구를 한 창의 탭으로 전환하는 통합 실행 파일 (`Ctrl+1/2/3` 지원, 현재 탭은 별도 창으로 분리 가능)
- `pcg_st9_texture_batch/PCG_ST9_Texture_Batch.bat`: PCG에서 사용하는 ST9 나무를 SK·나나이트·버추얼 텍스처 작업으로 연결하는 준비 보드
- `sk_batch/SK_Batch.bat`: SPM 수정, Blender 리페어, Unreal 전송을 단계별로 실행하는 SK 식생 배치 도구
- `spm_generator_sync/SPM_Generator_Sync.bat`: 같은 수종의 SPM을 마스터·자식·독립 계보로 관리하고 Base 제작 구조와 아이콘 색을 동기화하는 도구. 이후 SK Batch의 `0. Generator Sync`가 같은 엔진을 호출할 수 있는 패키지 진입점을 포함합니다.

각 도구의 상세 사용법은 해당 폴더의 `README.md`를 참고합니다.

## 프로세스 간 공용 실행 대기열

세 GUI의 **변경 작업**은 창마다 따로 실행되지 않고
`%LOCALAPPDATA%\SpeedTreeBatchTools\shared_job_queue.json`의 공용 FIFO에
등록된다. 통합 창의 다른 탭, 별도 BAT 창, 같은 도구를 두 번 연 창도 등록 순서를
공유하며 실제 에셋 작업은 한 번에 하나만 실행한다.

- SPM Generator Sync의 관계 적용·동기화·Cluster 갱신
- PCG ①/②/③, 전체 재추출, Atlas 대상 해제
- SK Batch의 SPM·Blender·Unreal·전체 자동 작업

대기 중인 창은 `공용 대기열 대기 · 전체 N번째`를 표시하며 UI 이벤트 루프를
막지 않는다. 실행 작업은 heartbeat lease를 유지한다. 실행 프로세스가 종료되면
해당 비멱등 작업을 자동 재실행하지 않고 `owner_lost` 실패로 기록한 뒤 다음
작업을 진행한다. 읽기 전용 검사와 표 갱신은 대기열 밖에서 계속 사용할 수 있다.

완료·실패·취소·abandoned 기록은 `terminal_at`이 가장 최신인 100개만 유지한다
(`terminal_at`이 없는 기존 행은 상태별 종료 시각과 sequence를 차례로 사용한다).
따라서 먼저 등록됐지만 늦게 끝난 작업의 새 감사 기록도 보존된다. 대기 또는 실행
중인 작업은 retention 대상이 아니며 자동으로 제거하지 않는다.

### 멈춘 공용 대기열 수동 해제

살아 있는 GUI 프로세스 안의 worker가 멈춰도 heartbeat thread는 lease를 계속
갱신할 수 있다. 이 경우 먼저 상태를 확인한다. 상태 출력은 payload, 파일 경로,
호스트/사용자 정보를 표시하지 않는다.

```powershell
python .\shared_job_queue.py status
```

실행 후 15분이 지난 live-owner job에만 해제 요청 안내가 표시된다. status에 나온
job ID를 두 번 같게 입력한다. 이 명령은 lease를 즉시 제거하지 않고
`operator_release_requested` 요청만 기록하므로 단순히 느린 정상 작업과 다음 작업이
겹쳐 실행되지 않는다.

```powershell
python .\shared_job_queue.py request-release <JOB_ID> --confirm-job-id <JOB_ID>
```

현재 lease token 소유자는 요청을 heartbeat에서 확인하고, 실제 worker를 중지한 뒤
join까지 끝낸 경우에만 `acknowledge_release()`를 호출한다. 그 ack가 성공해야
`owner_released_by_operator` terminal failure가 되고 다음 작업이 진행된다. 잘못된
token이나 request ID는 거부된다. 소유자가 ack할 수 없는 상태로 종료되면 lease 만료
후 기존의 `owner_lost` 경로만 사용한다. 요청 당시 token을 제외한 lease, requester,
owner ack 정보는 감사 필드에 보존한다. 기존 `force-release` 명령 이름은 호환 별칭이지만
동일하게 요청만 만든다. 실제 GUI에서 worker 중지·join·ack를 확인하는 acceptance는
운영 완료 전에 별도로 수행해야 한다.

### 상태 및 시작 오류 보존

- 읽을 수 없는 `sk_batch/sk_batch_state.json`은 빈 상태로 덮어쓰기 전에 같은
  폴더의 `sk_batch_state.unreadable-<UTC>-<id>.json`으로 격리하고,
  `sk_batch/logs/state_recovery.log`에 파일명과 `state_unreadable_quarantined` reason
  token만 기록한다. 읽기·격리·prune·재기록은 경로별 process mutex 안에서 수행하고,
  원본 bytes를 다시 확인해 다른 프로세스가 만든 정상 상태를 이동하거나 덮어쓰지 않는다.
- 저장 시 존재하지 않는 SPM과 pipeline backup 폴더의 상태 행은 제거한다. 일시적인
  접근 오류는 삭제 근거로 취급하지 않는다.
- `speedtree_batch_tools_error.log`는 파일당 256 KiB, backup 2개로 회전한다.
  stat·rotation·append 전체를 경로별 process mutex로 직렬화한다. 테스트는
  `SPEEDTREE_BATCH_TOOLS_ERROR_LOG`로 임시 경로를 사용해 저장소 로그를 오염시키지
  않는다.

## 공통 GUI 편의성

세 GUI의 행 활성화와 Everything용 경로 복사는 `batch_ui_common` 패키지를
단일 진실 공급원으로 사용합니다. 새 GUI 편의 기능은 각 `.pyw`에 복제하지 않고
공통 API와 테스트에 먼저 추가한 뒤, 도구별 행 데이터만 얇은 어댑터로 연결합니다.
세부 동작 계약과 적용 범위는 `batch_ui_common/README.md`에 정리되어 있습니다.

## 저장소 배치

기본 구성은 두 저장소를 같은 상위 폴더에 체크아웃하는 방식입니다.

```text
GitHub/
├─ speedtree-batch-tools/
└─ speedtree-bone-weight-repair-addon/
```

`sk_batch`는 형제 저장소의 SpeedTree 10.1 프리셋을 자동으로 찾습니다. 다른 위치에 둔 경우 `SPEEDTREE_BWR_ADDON_DIR` 환경 변수에 `speedtree_bone_weight_repair` 패키지 폴더 경로를 지정할 수 있습니다. 기존 `sk_batch_config.json`에 `fbx_ini`와 `xml_ini`가 저장되어 있으면 그 값이 우선 적용됩니다.

## 외부 의존성

- Python 3
- Blender 및 활성화된 `speedtree_bone_weight_repair` 애드온
- SpeedTree Modeler 10.1
- PCG 텍스처 작업용 Adobe Substance 3D Designer
- 아틀라스 생성 작업용 `atlas_leaf_mesh_builder` Blender 애드온
- Atlas blend별 `<blend stem>.atlas_leaf_targets.json` 대상 목록은 PCG 탭과 Blender 애드온이 공동 관리
- Unreal 전송 작업용 Send to Unreal 및 프로젝트 측 동적 바람 임포트 기능

개인 PC 경로가 들어가는 설정 JSON, 실행 상태, 로그, 생성 리포트는 로컬에는 유지되지만 Git에는 포함되지 않습니다.

실패 Blender/Unreal 재시도의 단계, liveness 시계, durable receipt, 안전 취소
경계는 [RETRY_PROGRESS_LIVENESS.md](RETRY_PROGRESS_LIVENESS.md)에 정리되어
있습니다. Sanitized before/after 증적은
[ISSUE_107_RETRY_PROGRESS_EVIDENCE.md](ISSUE_107_RETRY_PROGRESS_EVIDENCE.md)를
참조하세요.

## 테스트

통합 GUI와 SK Batch 직접 GUI는 실제 창을 import하기 전에 다음 빠른 코드·계약
게이트를 자동 실행합니다. 에셋, Blender, SpeedTree, Unreal은 실행하지 않습니다.

```powershell
python .\sk_batch\code_compile_gate.py
```

```powershell
python -m unittest discover -s .\tests -v
python -m unittest discover -s .\batch_ui_common\tests -v
python -m unittest discover -s .\pcg_st9_texture_batch\tests -v
python -m unittest discover -s .\sk_batch\tests -v
python -m unittest discover -s .\spm_generator_sync\tests -v
python -m compileall -q .\batch_ui_common .\pcg_st9_texture_batch .\sk_batch .\spm_generator_sync
```
