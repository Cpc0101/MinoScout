import Foundation
import Vision
import ImageIO

do {
    let url = URL(fileURLWithPath: CommandLine.arguments[1])
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { exit(2) }
    let request = VNRecognizeTextRequest()
    request.usesCPUOnly = true
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    request.recognitionLanguages = CommandLine.arguments[2].split(separator: ",").map(String.init)
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    let nodes: [[String: Any]] = (request.results ?? []).compactMap { result in
        guard let candidate = result.topCandidates(1).first else { return nil }
        let b = result.boundingBox
        let w = Double(image.width), h = Double(image.height)
        return ["text": candidate.string, "confidence": Double(candidate.confidence),
                "bounds": [b.minX * w, (1 - b.maxY) * h, b.maxX * w, (1 - b.minY) * h]]
    }
    let data = try JSONSerialization.data(withJSONObject: nodes)
    FileHandle.standardOutput.write(data)
} catch {
    FileHandle.standardError.write(Data(String(describing: error).utf8))
    exit(1)
}
