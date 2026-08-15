# SpeedTree post-collision CLI

SpeedTree Modeler 10.1.0의 기본 `-export` 명령이 잎 충돌 계산보다 먼저 실행되는 문제를 보완하는 Windows x64 전용 호출기입니다.

화면 좌표 클릭이나 고정식 UI 자동화는 사용하지 않습니다. 모델러를 포커스를 가져오지 않는 정상 GUI 모드로 초기화한 뒤, 설치 바이너리 내부의 모델 계산 경로와 `CCollisionThread` 완료 상태를 확인하고 `ExportCommandLineTree(...)`를 Qt 메인 스레드에서 직접 호출합니다. 최소화 상태에서는 SpeedTree의 `OnIdle/OnIdleDraw` 갱신이 정지하므로 작업 중 창을 최소화하지 않습니다.

## 동작 순서

1. 설치된 SpeedTree Modeler와 Qt DLL의 SHA-256을 검증합니다.
2. 모델러를 활성화하지 않는 표시 상태로 시작하고 helper DLL을 주입합니다.
3. SPM의 충돌 품질을 3으로 맞추고 정상 GUI 컨트롤러에 베이크를 요청합니다.
4. 실제 `CCollisionThread` 시작과 완료, 생성된 충돌 입력을 확인합니다.
5. 완료된 모델 상태에서 내부 `ExportCommandLineTree(output, preset, game)`를 호출합니다.
6. 이 함수가 지오메트리를 재생성하며 시작하는 두 번째 충돌 갱신은 내보내기 스레드에서 동기 실행합니다. 이 단계가 잎 충돌뿐 아니라 브랜치의 `Shade Pruning` 결과도 FBX에 남깁니다.
7. FBX가 만들어지지 않거나 계산이 확인되지 않으면 unculled FBX를 성공으로 처리하지 않습니다.

설치된 SpeedTree 파일과 입력 SPM은 수정하지 않습니다.

## Persistent 세션

여러 SPM을 연속 처리할 때는 하나의 SpeedTree 프로세스를 재사용할 수 있습니다. 시작 시 `blank.spm` 하나를 anchor 문서로 열어 두고, 각 작업마다 내부 Open 경로에 대상 SPM을 직접 전달한 뒤 `load → quality 3 bake → export → 대상 탭 닫기`를 수행합니다. 파일 선택창, 화면 좌표 클릭, 드래그 앤 드롭은 사용하지 않습니다.

`SK_Batch.bat`와 통합 `SpeedTree_Batch_Tools.bat`의 기본 격리 모드는 각 SPM을 새 Modeler 프로세스의 시작 문서로 직접 전달합니다. SpeedTree 10.1은 비입력 desktop의 기존 프로세스에서 두 번째 SPM을 `fileOpen`으로 추가하면 계산을 시작하지 않으므로, 격리 모드에서는 멈춤을 피하기 위해 persistent 재사용을 자동으로 끕니다. `SPEEDTREE_COLLISION_ISOLATED_WINDOW=0`을 명시한 대화형 모드에서만 GUI 수명 동안 `--serve-session` 호스트를 하나 소유하며 named pipe로 요청을 직렬화합니다.

Modeler는 기본적으로 wrapper 전용 Windows desktop에서 `SW_SHOW`의 정상 GUI 상태로 실행됩니다. 그 desktop을 사용자의 input desktop으로 전환하지 않으므로 SpeedTree 내부의 활성 창·렌더·collision 경로는 유지하면서도 현재 화면, 포커스, 마우스 입력, 작업 표시줄과 Alt+Tab에는 노출되지 않습니다. 진단을 위해 사용자 desktop에 표시해야 할 때만 `--interactive-window` 또는 `SPEEDTREE_COLLISION_ISOLATED_WINDOW=0`을 사용합니다.

대화형 persistent host가 예기치 않게 닫히면 launch guard가 기본 3회까지 새 process tree로 재시작합니다. export 중 기존 named pipe가 끊기면 client는 죽은 세션을 성공으로 넘기지 않고 replacement host를 시작합니다. 기본 격리 모드를 포함해 Blender exporter는 Modeler crash 결과에 대해 최대 3회의 fresh staging 재시도를 수행하며, 모든 재시도가 실패한 경우에만 해당 export가 실패합니다. one-shot CLI는 프로세스 CPU·I/O·working set·page fault 또는 hook 로그 중 하나라도 의미 있게 변하면 계속 기다리지만 모든 신호가 기본 30초 동안 멈추면 정확히 자신이 실행한 Modeler만 종료하고 재시도 가능한 stall 결과를 반환합니다.

복구 질문은 두 단계로 차단합니다. 핵심 1차 차단은 SpeedTree의 `MainWindowRecoveryCheck` 원본을 아예 호출하지 않아 `QMessageBox` 객체와 Question 창 생성 경로 자체를 시작하지 않습니다. 특정 경로나 `.sbk` 이름은 판별하지 않습니다. 2차 안전망은 다른 내부 경로가 1차를 우회한 경우에만 Qt의 최종 `QDialog::exec()` 경계에서 `QMessageBox`의 `Question` 아이콘을 판별해 화면 표시 직전에 차단하고 `No`를 반환합니다. SpeedTree 자동저장 설정과 `.sbk` 복구 파일은 변경하거나 삭제하지 않으므로, 수동으로 연 Modeler의 복구 기능은 그대로 유지됩니다. `ShowNewOnStart` 레지스트리 값도 persistent 프로세스 초기화 직후 원래 값으로 복구합니다. 대상이 정상 로드되어도 SpeedTree 탭 이름이 잠시 `blank.spm`으로 남을 수 있지만 내부 모델과 FBX 출력에는 영향을 주지 않습니다.

```powershell
.\speedtree_collision_cli\bin\speedtree_collision_cli.exe `
  --persistent `
  --session-anchor "$env:USERPROFILE\Downloads\blank.spm" `
  -- "D:\path\tree.spm" `
  -export_options "D:\path\Options_MA_Fbx.ini" `
  -export "D:\path\tree.fbx"

.\speedtree_collision_cli\bin\speedtree_collision_cli.exe --shutdown-session
```

세션 상태만 확인하려면 다음 명령을 사용합니다.

```powershell
.\speedtree_collision_cli\bin\speedtree_collision_cli.exe --ping-session
```

`SK_tree_black_locast_03/04/05.spm`을 한 PID에서 연속 처리한 회귀 테스트에서도 세 작업의 FBX 생성과 대상 탭 닫기가 모두 완료됐습니다.

`SK_bush_black_locast_01.spm` 검증 결과는 수동 GUI 내보내기와 동일한 33,488 버텍스 / 46,384 트라이앵글이었습니다. 네 재질의 삼각형 집합도 모두 완전히 일치했습니다.

## 빌드

Visual Studio 2022 Community의 C++ 도구가 필요합니다.

```powershell
.\speedtree_collision_cli\build.ps1
```

## 설치 버전 진단

```powershell
.\speedtree_collision_cli\bin\speedtree_collision_cli.exe --diagnose
```

## 사용법

기존 SpeedTree CLI 인자를 그대로 전달합니다.

```powershell
.\speedtree_collision_cli\bin\speedtree_collision_cli.exe -- `
  "D:\path\tree.spm" `
  -export_options "D:\path\Options_MA_Fbx.ini" `
  -export "D:\path\tree.fbx"
```

호출기 옵션:

- `--timeout-ms 600000`: 베이크 최대 대기 시간
- `--log D:\path\collision_hook.log`: 상세 로그 경로
- `--modeler D:\path\SpeedTree_Modeler.exe`: 모델러 경로 재정의
- `--persistent`: 하나의 blank-anchored SpeedTree 프로세스 재사용
- `--session-anchor D:\path\blank.spm`: persistent 프로세스가 유지할 anchor SPM
- `--isolated-window`: Modeler를 전용 Windows desktop에서 정상 활성 상태로 격리(기본값)
- `--interactive-window`: 현재 화면에 Modeler 표시(진단 전용)
- `--stall-timeout-ms 30000`: CPU/I/O/메모리/hook 로그 진행이 모두 멈춘 상태의 최대 허용 시간
- `--shutdown-session`: 실행 중인 persistent 프로세스 종료
- `--no-persistent`: 환경 변수 설정과 관계없이 기존 one-shot 경로 사용

## 지원 빌드

현재 구현은 다음 설치 바이너리에만 열립니다.

- SpeedTree Modeler 10.1.0 SHA-256: `ED552D9B138690BC9D0812128876066B49A078310B70D84BA6D9459DDA7AF441`
- bundled Qt6Core 6.6.0 SHA-256: `FE3C6E86E01ACFCACDD9939031D501E6E6237999B99D17F26853A5EE2CCAF959`

SpeedTree가 업데이트되면 내부 RVA, 함수 prologue, Qt ABI를 다시 검증해야 합니다. 해시가 다르면 DLL 주입 전에 중단합니다.
