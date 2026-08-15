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

Modeler 창, 파일 선택창, recovery Question, blank 문서, 마우스 포커스,
Windows desktop 격리는 생산 경로에서 사용하지 않습니다.

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

Blender Bone Weight Repair add-on은 두 산출물이 동시에 stale일 때 이 묶음
경로를 자동 사용합니다. 각 산출물의 content cache는 독립적으로 유지되므로
한쪽만 stale이면 그 파일만 내보냅니다.

SK Batch의 ① 뼈 검증처럼 임시 FBX/XML에서 뼈와 geometry 존재 여부만
확인하는 경로는 `--verification-only`를 사용합니다. 이 옵션은 원래 CLI
직렬화와 한 프로세스 이중 출력은 유지하지만, 최종 자산에서만 필요한
Collision/Prune 재계산은 수행하지 않습니다. 생산 FBX 내보내기에는 이 옵션을
사용하지 않으므로 High Collision/Prune 계약에는 영향이 없습니다.

## SPM 정규화

SK Batch의 `spm_audit.py`는 재질 `M_` 이름 보정과 같은 비-bone 변환 단계에서
새 SPM을 다음 값으로 한 번 정규화합니다.

- `<m_eCollisionQuality>3</m_eCollisionQuality>`
- `<m_bShadePruning>true</m_bShadePruning>`

기존 전체 폴더를 Modeler 실행 없이 정규화할 수도 있습니다. 변경 전 원본은
각 폴더의 `_spm_backups`에 보존됩니다.

```powershell
python .\sk_batch\spm_audit.py `
  --normalize-collision-pruning-only `
  --recursive-root "D:\OneDrive\Forestportfolio\02_nature\Tree"
```

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

주요 옵션:

- `--timeout-ms 600000`: 전체 계산 최대 시간
- `--stall-timeout-ms 30000`: CPU/I/O/메모리/로그가 모두 멈춘 시간 제한
- `--log D:\path\collision_hook.log`: 상세 로그
- `--modeler D:\path\SpeedTree_Modeler.exe`: 설치 경로 재정의
- `--verification-only`: 임시 감사 출력에서 Collision/Prune bake 생략
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
