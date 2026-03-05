class Differ < Formula
  include Language::Python::Shebang

  desc "Terminal UI for monitoring multiple local Git repositories"
  homepage "https://github.com/MartinWie/Differ"
  url "https://github.com/MartinWie/Differ/archive/refs/tags/v0.1.1.tar.gz"
  sha256 "ba6ed7cfa16bf73194ab11765b6fe9a519244c5f54a147fbd15dbf5653d0abfd"
  license "MIT"

  depends_on "python@3.12"

  def install
    (libexec/"differ.py").write (buildpath/"differ.py").read
    rewrite_shebang detected_python_shebang, libexec/"differ.py"
    chmod 0755, libexec/"differ.py"
    bin.install_symlink libexec/"differ.py" => "differ"
  end

  test do
    assert_equal version.to_s, shell_output("#{bin}/differ --version").strip
  end
end
