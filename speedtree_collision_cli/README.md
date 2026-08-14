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

## 지원 빌드

현재 구현은 다음 설치 바이너리에만 열립니다.

- SpeedTree Modeler 10.1.0 SHA-256: `ED552D9B138690BC9D0812128876066B49A078310B70D84BA6D9459DDA7AF441`
- bundled Qt6Core 6.6.0 SHA-256: `FE3C6E86E01ACFCACDD9939031D501E6E6237999B99D17F26853A5EE2CCAF959`

SpeedTree가 업데이트되면 내부 RVA, 함수 prologue, Qt ABI를 다시 검증해야 합니다. 해시가 다르면 DLL 주입 전에 중단합니다.
