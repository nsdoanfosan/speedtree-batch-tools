# SK Vegetation Batch

SpeedTree SPM을 Blender Assembly와 Unreal 전달 산출물로 처리하는 배치 도구다.
`SK_Batch.bat`를 실행하면 검사 후 `① Assembly → ② Unreal Push` 순서로 동작한다.

## 현재 파이프라인

### 검사

- SPM, Blend, Assembly 보고서, wind JSON과 관계 산출물의 현재성만 판정한다.
- 파일의 크기·수정 시각·NTFS change time·file ID가 같은 경우 저장된 검증 결과를
  즉시 재사용한다.
- 관계 보고서에 같은 산출물이 여러 번 나타나도 파일은 한 번만 검사한다.
- 검사에서는 SPM 본을 파싱하거나 본 수·weight를 추정하지 않는다.
- 5초 미만의 빠른 감사에는 heartbeat 로그를 출력하지 않는다.

### ① Assembly

- SpeedTree의 개조 native exporter가 SPM에 저장된 정확한 본 연결과 skin weight를
  FBX/XML에 직접 직렬화한다.
- nearest bone, 거리 기반 근사, 0-weight 자동 할당, weight 보정, BaseRef 본 복원은
  수행하지 않는다.
- Blender 단계는 이미 직렬화된 rig를 변경하지 않고 Assembly, 재질, wind와
  Cluster 관계 산출물을 만든다.
- 현재 영수증이 유효한 Blend는 Blender를 시작하기 전에 건너뛴다.
- Cluster 관계 감사의 authoritative child 결과를 부모가 다시 해시하지 않는다.

### ② Unreal Push

- 확정된 Assembly 산출물과 wind JSON을 Unreal로 전달한다.
- 같은 전체 실행에서 ①이 확정한 계약을 재사용하며 동일한 전체 감사를 반복하지
  않는다.
- `headless` transport는 export 후 Unreal import를 순차 처리한다.
- `unreal_wait` transport는 export manifest를 보존하고 Editor가 종료된 뒤 대기
  에셋을 한 세션에서 가져온다.

## 병렬 실행

무거운 프로세스 두 개가 동시에 실행되면 16 논리 코어 시스템에서 각 프로세스에
서로 겹치지 않는 8코어 affinity를 배정한다. 따라서 설정값 `cores=8`은 전체 실행이
8코어로 제한된다는 뜻이 아니라 작업 하나의 상한이다. SpeedTree처럼 동시에 하나만
실행되는 프로세스는 해당 작업의 affinity 범위를 사용한다.

## 캐시와 영수증

- 영수증 JSON 자체는 작다. 이전 구현은 영수증이 참조한 대용량 FBX/XML/Atlas를
  매번 다시 해시해 영수증이 큰 것처럼 보였으나, 현재는 동일 NTFS identity를 먼저
  확인하고 바뀐 파일만 읽는다.
- 시작할 때 수천 개의 종료 영수증을 전부 파싱하지 않는다. 활성 인덱스를 사용하고
  종료 이력은 한 번에 512개로 정리한다.
- 캐시는 힌트일 뿐이다. 같은 크기와 수정 시각을 유지한 채 내용이 바뀌어도 NTFS
  change time이 달라져 무효화된다.

## 실행과 진단

```powershell
.\sk_batch\SK_Batch.bat
python .\sk_batch\code_compile_gate.py
```

한 자산을 Unreal까지 정확히 다시 넣을 때:

```powershell
.\sk_batch\SK_Exact_Push.bat --transport rpc --spm "D:\path\SK_tree_01.spm"
```

관련 로그와 영수증은 각 작업이 출력한 경로를 따른다. 실패 시 자동 추정이나 본
보정으로 통과시키지 않으며, 원본 exporter/Assembly 계약에서 누락된 정확한 증거를
보고한다.
