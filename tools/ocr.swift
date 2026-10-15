import Vision
import AppKit

// Print every recognized text line with its center point (x, y from the top-left), tab separated.
// The accurate recognizer failed on 2026-10-15 (macOS 27.2 beta: "e5rtError" in TextRecognition); the fast one works.
// So: accurate first, and the fast recognizer when that throws.
let path = CommandLine.arguments[1]
guard let img = NSImage(contentsOfFile: path),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { exit(1) }

func recognize(_ level: VNRequestTextRecognitionLevel) throws -> [VNRecognizedTextObservation] {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = level
    req.recognitionLanguages = ["en-US"]
    try VNImageRequestHandler(cgImage: cg).perform([req])
    return req.results ?? []
}

var results: [VNRecognizedTextObservation] = []
do {
    results = try recognize(.accurate)
} catch {
    results = try recognize(.fast)
}
let w = Double(cg.width), h = Double(cg.height)
for obs in results {
    if let t = obs.topCandidates(1).first {
        let b = obs.boundingBox
        print("\(t.string)\t\(Int(b.midX * w))\t\(Int((1 - b.midY) * h))")
    }
}
