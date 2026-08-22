# SpeedTree collision/pruning CLI extension

SpeedTree Modeler 10.1.0의 공식 `-export` 경로가 Collision과 Shade Pruning의
최종 generator commit 전에 FBX를 직렬화하는 문제를 보완하는 Windows x64
호출기입니다.

기본 경로는 UI 자동화가 아닙니다. Modeler의 원래 명령행 인자를 그대로
전달하고 창을 만들지 않는 상태(`SW_HIDE`)로 시작한 뒤, 버전 고정 helper DLL이
공식 CLI의 모델 계산 단계에 아래 작업만 추가합니다.

1. 모델 전역 설정을 Collision On / High(`m_eCollisionQuality=3`)와 Shade
   Pruning On으로 고정합니다.
2. 화면 없는 OpenGL context에서 shade volume과 collision 입력을 계산합니다.
3. Modeler의 collision 완료 scheduler와 generator commit을 동기 실행합니다.
4. UI 전용 알림/뷰 callback만 계산 구간 동안 우회합니다.
5. 최종 generator geometry를 공식 exporter가 FBX/XML로 기록하게 합니다.
6. Modeler가 파싱한 BaseRef 연결을 공통 export bone graph에 다시 기록한 뒤
   FBX/XML serializer가 같은 정확한 계층을 사용하게 합니다.
7. FBX serializer가 이미 계산한 ID 0/root influence와 단일 root의 Start
   cluster를 deform bone으로 보존합니다.
8. 같은 serializer 호출에서 geometry/local vertex, runtime Node/Generator GUID,
   authored position과 그 위치의 원본 bone influence를 native receipt로 기록합니다.

Modeler 창, 파일 선택창, recovery Question, blank 문서, 마우스 포커스,
Windows desktop 격리는 생산 경로에서 사용하지 않습니다.

## BaseRef 본 계층 복원

SpeedTree 10.1.0은 SPM을 연 뒤 런타임 node graph에는 BaseRef 연결을 유지하지만,
export bone record를 만들 때 일반 parent 조회가 Base node에서 끊겨 해당 branch의
첫 본을 parent ID 0으로 기록합니다. FBX와 XML이 같은 잘못된 임시 bone graph를
사용하므로 serializer 옵션이나 공식 CLI 사용 여부로는 해결되지 않습니다.

이 확장은 두 serializer보다 앞선 공통 bone-record 삽입 지점을 version-locked
hook으로 보완합니다. 끊긴 record마다 Modeler가 이미 파싱한 다음 연결만 사용합니다.

1. child `CBranchNode`의 실제 parent `CBaseNode`
2. `CBaseNode`가 보유한 paired `CBaseRefNode`와 target `CBranchNode`
3. child node에 저장된 anchor index, anchor record, branch offset, section
4. target branch의 Modeler 원본 bone-ID resolver

target branch와 BaseRef의 역참조가 정확히 일치할 때만 원본 resolver가 반환한
parent ID를 기록합니다. 가장 가까운 본, 좌표 tolerance, 이름 유사도, scale 추정,
외부 mapping은 사용하지 않습니다. 참조 체인이나 anchor record가 불완전하거나
resolver가 유효한 ID를 반환하지 못하면 export를 실패시키며 근사값으로 진행하지
않습니다.

`SK_Tree_elm_01.spm` 검증에서는 누락된 BaseRef edge 305개가 모두 복원됐습니다.
XML의 305개 ParentID는 내부 resolver 결과와 전부 일치했고, Blender 5.1 FBX
재임포트에서도 305개가 모두 부모를 가지며 BaseRef root로 남은 항목은 없었습니다.

## FBX root weight 보존

SpeedTree 10.1.0의 FBX weight 함수는 각 vertex의 source bone ID와 위치, 원본
export bone record를 사용해 최대 두 influence를 계산합니다. SPM 밖에서 가장 가까운
본을 찾는 과정이 아니라 Modeler 자체의 계산입니다. 이 함수와 생성된 FBX cluster를
추적하면 누락된 값도 이미 내부에 존재합니다.

- source bone ID가 0인 vertex는 `Root` cluster에 weight 1을 기록합니다.
- 첫 실제 본과 ID 0 사이의 vertex는 실제 본 weight를 계산한 뒤 그 보수값을
  ID 0 `Root` cluster에 기록해야 합니다.
- 그러나 10.1.0은 parent ID가 0이면 보수값을 계산한 직후 조기 반환합니다.
- `Root` wrapper도 특수 `eRoot` node라 Blender 같은 importer가 deform bone 대신
  armature container로 소비하여, 기록된 ID 0 cluster가 vertex group으로 남지 않습니다.
- 정확한 BaseRef graph가 top-level bone을 하나로 줄이면 wrapper 자체도 만들지 않아
  `Bone_1_Start` cluster까지 같은 방식으로 유실됩니다.

확장은 Modeler의 기존 wrapper를 top-level bone이 하나일 때도 만들고 이를
`eLimbNode`로 직렬화합니다. parent ID 0 조기 반환에서는 Modeler가 방금 계산한
child weight와 동일한 단정도 연산의 보수값을 Modeler 원본 ID 0 cluster 생성 경로로
전달합니다. vertex 좌표 근접 검색, 가장 가까운 본, 이름 매칭, weight 정규화,
0-weight 대체값은 사용하지 않습니다.

`SK_Tree_elm_01.spm`의 실제 FBX 재임포트 검증 결과는 다음과 같습니다.

- mesh vertex 105,504개
- weight가 없는 vertex 0개
- weight 합이 1이 아닌 vertex 0개
- 1보다 큰 vertex 0개
- bone이 아닌 vertex group으로 향한 양수 weight 0개
- BaseRef 305개 XML parent 불일치 0개, FBX orphan 0개

## Native runtime receipt

`--native-receipt`는 Modeler가 SPM을 이미 파싱한 뒤 FBX를 직렬화하는 바로 그
호출에서 후속 Assembly 식별값을 기록합니다. Python이나 Blender가 SPM/XML을
다시 열어 본·Node·Generator·배치 프록시를 복원하지 않습니다.

```powershell
.\speedtree_collision_cli\bin\speedtree_collision_cli.exe `
  --native-receipt "D:\out\tree.speedtree_native_receipt.json" `
  "D:\path\tree.spm" `
  -export_options "D:\path\Options_MA_Fbx.ini" `
  -export "D:\out\tree.fbx"
```

영수증에는 다음 exact identity만 들어갑니다.

- serializer geometry ordinal과 정확한 local vertex 범위
- 실제 runtime Node/parent/Generator GUID와 authored position
- Modeler 원본 weight solver가 authored position에 반환한 양수 influence
- 실제 생성된 FBX cluster node 이름과 native bone ID/parent ID

Assembly는 보존된 geometry/local vertex ID와 이 범위가 교차하는 runtime Node가
정확히 하나일 때만 바인딩합니다. 여러 Node가 교차하면 순위를 매기지 않고
거부합니다. 잘린 FBX 부분집합에서 authored origin vertex가 사라져도 surviving
local vertex가 단 하나의 runtime Node 범위를 증명하면 그 원본 authored position과
weight를 그대로 사용합니다. 최근접 검색, 비율 투표, 이름 매칭, weight 재계산은
없습니다.

## 사용법

기존 SpeedTree CLI 인자를 그대로 넘깁니다. `--native-cli`는 기본값이므로
생략할 수 있습니다.

```powershell
.\speedtree_collision_cli\bin\speedtree_collision_cli.exe `
  "D:\path\tree.spm" `
  -export_options "D:\path\Options_MA_Fbx.ini" `
  -export "D:\path\tree.fbx"
```

FBX와 XML이 모두 필요하면 한 Modeler 프로세스에서 연속 내보낼 수 있습니다.

```powershell
.\speedtree_collision_cli\bin\speedtree_collision_cli.exe `
  --secondary-export-options "D:\path\Options_HI_Xml.ini" `
  --secondary-export "D:\out\tree.xml" `
  "D:\path\tree.spm" `
  -export_options "D:\path\Options_MA_Fbx.ini" `
  -export "D:\out\tree.fbx"
```

두 형식을 함께 내보낼 때 High Collision/Prune 계산과 generator commit은 첫
산출물에서 한 번만 수행합니다. 두 번째 serializer는 변경되지 않은 committed
model을 그대로 사용하므로 XML을 위해 같은 Collision thread를 다시 돌리지 않습니다.

CLI hook은 SPM의 이전 on/off 값을 신뢰하지 않고 런타임에도 동일 값을 강제합니다.
따라서 아직 정규화되지 않은 입력도 잘못된 unpruned FBX로 성공 처리되지 않습니다.

## 빌드 및 진단

```powershell
.\speedtree_collision_cli\build.ps1 -IfNeeded
.\speedtree_collision_cli\bin\speedtree_collision_cli.exe --diagnose
```

BAT 실행기는 매번 이 빠른 freshness 검사를 거칩니다. 소스 또는 프로토콜이
바이너리보다 새로울 때만 한 번 재빌드하고, 같은 빌드의 다음 실행부터는 즉시
통과합니다.

각 SpeedTree 자식 프로세스에는 `RLM_CONNECT_TIMEOUT=1`과 CLI 전용 fail-fast
패치를 임시 적용합니다. 응답하지 않는 로컬 RLM 엔드포인트의 접속 시도 예산만
5에서 0으로 만들어 소켓을 열기 전에 기존 재시도 소진 경로로 넘기고, 접속 실패
및 캐시 라이선스 판정은 원래 흐름을 그대로 사용합니다. 패치는 자식 메모리에만
존재하며 설치된 Modeler EXE와 사용자 또는 시스템 환경변수는 바뀌지 않습니다.

주요 옵션:

- `--timeout-ms 600000`: CPU/메모리 변화와 무관한 전체 프로세스 절대 최대 시간
- `--stall-timeout-ms 30000`: CPU/I/O/메모리/로그가 모두 멈춘 시간 제한
- `SPEEDTREE_COLLISION_WRAPPER_TIMEOUT_MS`: 호출자가 같은 절대 최대 시간을
  환경변수로 전달할 때 사용하며, 명시한 `--timeout-ms`가 우선합니다.
- `--log D:\path\collision_hook.log`: 상세 로그
- `--modeler D:\path\SpeedTree_Modeler.exe`: 설치 경로 재정의
- `--verification-only`: 임시 감사 출력에서 Collision/Prune bake 생략
- `--native-receipt D:\path\tree.speedtree_native_receipt.json`: Modeler 런타임/
  FBX serializer identity 기록
- `--gui-bake`: 이전 GUI 기반 구현을 명시적으로 사용하는 진단 전용 옵션

`--persistent`, `--session-anchor`, `--isolated-window`는 이전 GUI 호환 경로에만
남아 있습니다. 기본 네이티브 CLI에서는 무시되며 BAT도 이를 설정하지 않습니다.

## 검증 기준

`SK_bush_black_locast_01.spm`의 기준 결과는 33,488 vertices / 46,384
triangles입니다. 브랜치 10,730, bark 27,528, cluster 5,794 / 2,332로 수동 GUI
베이크 결과와 일치합니다.

지원 설치:

- SpeedTree Modeler 10.1.0 SHA-256:
  `ED552D9B138690BC9D0812128876066B49A078310B70D84BA6D9459DDA7AF441`
- Qt6Core 6.6.0 SHA-256:
  `FE3C6E86E01ACFCACDD9939031D501E6E6237999B99D17F26853A5EE2CCAF959`
